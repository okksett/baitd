"""Ring buffer of the last minutes of a live stream, kept as MPEG-TS segment files.

`cut` joins the segments around a moment into an MP4 without re-encoding.
"""
from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg

BUFFER_S = 300.0
REVIEW_PAD_S = 20.0
GAP_TOLERANCE_S = 0.5


class NoFootage(Exception):
    """The buffer holds nothing for the requested moment."""


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

    def add(self, seq: int, start: float, duration: float, data: bytes) -> None:
        path = self.folder / f"{seq:010d}.ts"
        path.write_bytes(data)
        seg = _Seg(seq, start, duration, path)
        with self._lock:
            keys = [s.start for s in self._segs]
            self._segs.insert(bisect.bisect_right(keys, start), seg)
            self._prune()

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
            listing.write_text(
                "".join(f"file '{s.path.resolve().as_posix()}'\n" for s in chosen),
                encoding="utf-8",
            )
            try:
                ffmpeg.run(
                    ["-f", "concat", "-safe", "0", "-i", str(listing),
                     "-c", "copy", "-movflags", "+faststart", str(out)],
                    cancel=cancel, low_priority=True,
                )
            finally:
                listing.unlink(missing_ok=True)
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
