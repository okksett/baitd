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

    def cut(self, t0: float, t1: float, out: Path, pad: float = REVIEW_PAD_S) -> Cut:
        with self._lock:
            chosen = [s for s in self._segs if s.end > t0 - pad and s.start < t1 + pad]
            if not chosen:
                raise NoFootage("no footage for that moment")
            # Hold the lock while FFmpeg reads the files so pruning cannot delete them.
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
                    low_priority=True,
                )
            finally:
                listing.unlink(missing_ok=True)

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
        return Cut(out, file_time(t0), file_time(t1), total, gap)
