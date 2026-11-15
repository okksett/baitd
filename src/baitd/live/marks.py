"""Hotkey mark windows: what a press saves, how it extends and when it closes.

Pure logic: no I/O and no clock. Times are stream-time epoch seconds given
by the caller. The hotkey thread calls `press` while the manager's tick calls
`ready`, so the state is guarded by a lock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace

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
        self._closed: list[Mark] = []
        self._lock = threading.Lock()

    def press(self, t: float) -> Mark:
        """Open, extend or (when the open mark already ended) queue it and open a new one."""
        with self._lock:
            p = self._pending
            if p is not None and t > p.end:
                self._closed.append(p)
                p = None
            if p is None:
                p = self._pending = Mark(t - self.before_s, t + self.after_s)
            else:
                p.end = min(p.end + EXTEND_S, p.start + MAX_MARK_S)
                p.presses += 1
            return replace(p)

    def pending(self) -> Mark | None:
        """A copy of the open mark."""
        with self._lock:
            return replace(self._pending) if self._pending is not None else None

    def ready(self, head: float | None) -> Mark | None:
        """Remove and return the oldest mark whose end <= head; call until None."""
        if head is None:
            return None
        with self._lock:
            if self._closed and self._closed[0].end <= head:
                return self._closed.pop(0)
            if not self._closed and self._pending is not None and self._pending.end <= head:
                p, self._pending = self._pending, None
                return p
            return None

    def flush(self) -> list[Mark]:
        """Remove and return every mark, oldest first."""
        with self._lock:
            out = self._closed + ([self._pending] if self._pending is not None else [])
            self._closed = []
            self._pending = None
            return out

    def clamp_start(self, tail: float | None) -> None:
        """Move starts up to the buffer tail when the buffer is shorter."""
        if tail is None:
            return
        with self._lock:
            for m in self._closed + ([self._pending] if self._pending is not None else []):
                if tail > m.start:
                    m.start = tail

    def earliest_start(self) -> float | None:
        with self._lock:
            marks = self._closed + ([self._pending] if self._pending is not None else [])
            return min((m.start for m in marks), default=None)
