"""Hotkey mark windows: what a press saves, how it extends and when it closes.

Pure logic: no I/O and no clock. Times are stream-time epoch seconds given
by the caller. The hotkey thread calls `press` while the manager's tick calls
`ready`, so the state is guarded by a lock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

EXTEND_S = 15.0
MAX_MARK_S = 180.0


@dataclass
class Mark:
    start: float
    end: float
    presses: int = 1


class MarkTracker:
    def __init__(self, before_s: float = 45.0, after_s: float = 15.0) -> None:
        self.before_s = before_s
        self.after_s = after_s
        self._pending: Mark | None = None
        self._lock = threading.Lock()

    def press(self, t: float) -> Mark:
        with self._lock:
            p = self._pending
            if p is None:
                p = self._pending = Mark(t - self.before_s, t + self.after_s)
            elif t <= p.end:
                p.end = min(p.end + EXTEND_S, p.start + MAX_MARK_S)
                p.presses += 1
            return p

    def pending(self) -> Mark | None:
        with self._lock:
            return self._pending

    def ready(self, head: float | None) -> Mark | None:
        with self._lock:
            p = self._pending
            if p is not None and head is not None and head >= p.end:
                self._pending = None
                return p
            return None

    def flush(self) -> Mark | None:
        with self._lock:
            p, self._pending = self._pending, None
            return p

    def clamp_start(self, tail: float | None) -> None:
        """Move the start up to the buffer tail when the buffer is shorter."""
        with self._lock:
            p = self._pending
            if p is not None and tail is not None and tail > p.start:
                p.start = tail
