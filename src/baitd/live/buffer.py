"""Ring buffer of the last minutes of a live stream, kept as MPEG-TS or fragmented-MP4 segment files.

`cut` joins the segments around a moment into an MP4 without re-encoding.
"""
from __future__ import annotations

import bisect
import shutil
import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg

BUFFER_S = 300.0
REVIEW_PAD_S = 20.0
GAP_TOLERANCE_S = 0.5


class NoFootage(Exception):
    """The buffer holds nothing for the requested moment."""


def _init_len(path: Path) -> int:
    """Bytes of the init segment (ftyp + moov boxes) at the start of a stored fMP4 file."""
    pos = 0
    with open(path, "rb") as f:
        while True:
            head = f.read(8)
            if len(head) < 8:
                return pos
            size, kind = struct.unpack(">I4s", head)
            if kind not in (b"ftyp", b"moov") or size < 8:
                return pos
            pos += size
            f.seek(pos)


@dataclass
class Cut:
    path: Path
    start: float  # mark start, in seconds of the file
    end: float  # mark end, in seconds of the file
    duration: float
    gap: bool  # a segment is missing inside the mark (an ad or a lost segment)
    spans: tuple[tuple[float, float], ...] = ()  # (start, duration) of each joined segment, in file order

    def buffer_time(self, t: float) -> float:
        """The buffer-clock time of second `t` of the file, across any gap between the joined segments."""
        acc = 0.0
        for start, duration in self.spans:
            if t < acc + duration:
                return start + max(t - acc, 0.0)
            acc += duration
        return self.spans[-1][0] + self.spans[-1][1] if self.spans else t


@dataclass
class _Seg:
    seq: int
    start: float
    duration: float
    path: Path

    @property
    def end(self) -> float:
        return self.start + self.duration


class RingBuffer:
    def __init__(self, folder: Path, keep_s: float = BUFFER_S):
        self.folder = Path(folder)
        self.keep_s = keep_s
        self._segs: list[_Seg] = []  # sorted by start
        self._pin: float | None = None
        self._in_use: dict[int, int] = {}  # seq -> running cuts reading it
        self._lock = threading.Lock()
        self.folder.mkdir(parents=True, exist_ok=True)

    def add(self, seq: int, start: float, duration: float, data: bytes, ext: str = "ts") -> Path:
        path = self.folder / f"{seq:010d}.{ext}"
        path.write_bytes(data)
        seg = _Seg(seq, start, duration, path)
        with self._lock:
            keys = [s.start for s in self._segs]
            self._segs.insert(bisect.bisect_right(keys, start), seg)
            self._prune()
        return path

    def _prune(self) -> None:
        head = max(s.end for s in self._segs)
        while self._segs:
            oldest = self._segs[0]
            if head - oldest.end <= self.keep_s:
                break
            if self._pin is not None and oldest.end > self._pin:
                break
            if oldest.seq in self._in_use:
                break  # a cut is reading it; it is pruned when the cut releases it
            self._segs.pop(0)
            oldest.path.unlink(missing_ok=True)

    @staticmethod
    def _parts(chosen: list[_Seg], out: Path, temps: list[Path]) -> list[Path]:
        """The files FFmpeg joins: each MPEG-TS segment as is; each run of fMP4 segments with the same
        init and consecutive numbers as one fragmented MP4 (the init once, then the fragments).

        The concat demuxer cannot join fMP4 files: it takes each file's end timestamp as its duration,
        so every file after the first is shifted by the time already elapsed (a 6 s cut came out at
        14 s). Joining the fragments by bytes keeps their timestamps; a gap between runs is closed
        afterwards, as it is for MPEG-TS."""
        parts: list[Path] = []
        run: list[_Seg] = []
        run_init = b""

        def flush() -> None:
            nonlocal run
            if not run:
                return
            tmp = out.with_name(f"{out.stem}.run{len(parts)}.raw.mp4")
            temps.append(tmp)
            with open(tmp, "wb") as w:
                for i, seg in enumerate(run):
                    n = _init_len(seg.path)
                    with open(seg.path, "rb") as r:
                        if i:
                            r.seek(n)
                        shutil.copyfileobj(r, w)
            parts.append(tmp)
            run = []

        for seg in chosen:
            if seg.path.suffix != ".mp4":
                flush()
                parts.append(seg.path)
                continue
            n = _init_len(seg.path)
            with open(seg.path, "rb") as r:
                init = r.read(n)
            if run and (init != run_init or seg.seq != run[-1].seq + 1):
                flush()
            run_init = init
            run.append(seg)
        flush()
        return parts

    @staticmethod
    def _remux(raw: Path, cancel, temps: list[Path]) -> Path:
        done = raw.with_name(raw.name.replace(".raw.mp4", ".mp4"))
        temps.append(done)
        ffmpeg.run(["-i", str(raw), "-c", "copy", str(done)], cancel=cancel, low_priority=True)
        return done

    def head(self) -> float | None:
        with self._lock:
            return max((s.end for s in self._segs), default=None)

    def tail(self) -> float | None:
        with self._lock:
            return self._segs[0].start if self._segs else None

    def seconds(self) -> float:
        with self._lock:
            return sum(s.duration for s in self._segs)

    def pin(self, t: float | None) -> None:
        with self._lock:
            self._pin = t

    def clear(self) -> None:
        with self._lock:
            for s in self._segs:
                s.path.unlink(missing_ok=True)
            self._segs.clear()
            self._pin = None

    def cut(self, t0: float, t1: float, out: Path, pad: float = REVIEW_PAD_S,
            cancel: threading.Event | None = None) -> Cut:
        """Join the segments around [t0, t1] into `out`. Setting `cancel` stops FFmpeg (JobCancelled)."""
        with self._lock:
            chosen = [s for s in self._segs if s.end > t0 - pad and s.start < t1 + pad]
            if not chosen:
                raise NoFootage("no footage for that moment")
            for s in chosen:
                self._in_use[s.seq] = self._in_use.get(s.seq, 0) + 1
        try:
            # FFmpeg runs outside the lock so the capture thread is never stalled.
            out = Path(out)
            out.parent.mkdir(parents=True, exist_ok=True)
            listing = out.with_name(out.stem + ".concat.txt")
            temps: list[Path] = []
            try:
                parts = self._parts(chosen, out, temps)
                if len(parts) == 1:
                    ffmpeg.run(
                        ["-i", str(parts[0]), "-c", "copy", "-movflags", "+faststart", str(out)],
                        cancel=cancel, low_priority=True,
                    )
                else:
                    # each fMP4 run is remuxed on its own, so it starts at 0 and its duration is exact
                    parts = [self._remux(p, cancel, temps) if p in temps else p for p in parts]
                    listing.write_text(
                        "".join(f"file '{p.resolve().as_posix()}'" + chr(10) for p in parts),
                        encoding="utf-8",
                    )
                    ffmpeg.run(
                        ["-f", "concat", "-safe", "0", "-i", str(listing),
                         "-c", "copy", "-movflags", "+faststart", str(out)],
                        cancel=cancel, low_priority=True,
                    )
            finally:
                listing.unlink(missing_ok=True)
                for t in temps:
                    t.unlink(missing_ok=True)
        finally:
            with self._lock:
                for s in chosen:
                    n = self._in_use.get(s.seq, 0) - 1
                    if n > 0:
                        self._in_use[s.seq] = n
                    else:
                        self._in_use.pop(s.seq, None)
                if self._segs:
                    self._prune()

        total = sum(s.duration for s in chosen)

        def file_time(t: float) -> float:
            acc = 0.0
            for s in chosen:
                if t < s.start:
                    break
                if t < s.end:
                    acc += t - s.start
                    break
                acc += s.duration
            return min(max(acc, 0.0), total)

        inside = [s for s in chosen if s.start < t1 and s.end > t0]
        gap = any(
            b.start - (a.start + a.duration) > GAP_TOLERANCE_S
            for a, b in zip(inside, inside[1:])
        )
        return Cut(out, file_time(t0), file_time(t1), total, gap, tuple((s.start, s.duration) for s in chosen))
