"""Cheap signals that a moment of someone else's live stream is worth a look: the chat suddenly writing much
faster or laughing, and the sound getting much louder than usual. Each is compared with that stream's own
recent past, so a big chat and a small one are judged by their own pace.

The audio signal is loudness only (ruling R6): laughter in the audio is left to the AI. Nothing here is
heavy; the one FFmpeg call per segment runs below normal priority on its own thread.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import re
import statistics
import sys
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg

__all__ = ["BIN_S", "BASELINE_S", "MIN_HISTORY_S", "CHAT_RATE_X", "CHAT_MIN_MSGS", "LAUGH_SHARE", "LAUGH_MIN",
           "LAUGH_RATE_X", "LOUD_LU", "COOLDOWN_S", "LAUGH", "Spike", "ChatSignal", "AudioSignal", "segment_loudness",
           "LoudnessMeter", "below_normal_thread", "below_normal_scope"]

log = logging.getLogger(__name__)

BIN_S = 10.0  # width of a chat bin
BASELINE_S = 300.0  # history the baseline uses
MIN_HISTORY_S = 60.0  # history needed before any spike
CHAT_RATE_X = 2.0  # spike: this many times the baseline rate (calibrated; 3.0 to start)
CHAT_MIN_MSGS = 10  # spike: at least this many messages in the bin (calibrated; 8 to start)
LAUGH_SHARE = 0.35  # laughter spike: share of laughing messages
LAUGH_MIN = 5  # laughter spike: at least this many laughing messages
LAUGH_RATE_X = 4.0  # laughter spike also: this many times the baseline laughing messages (big chats)
LOUD_LU = 10.0  # audio spike: LU above the median (calibrated; 8.0 to start)
COOLDOWN_S = 45.0  # quiet time after a spike
SILENCE_LUFS = -70.0  # quieter segments count as this (ebur128's absolute gate), so silence cannot skew the median
METER_QUEUE = 32  # segments waiting to be measured; more are skipped

LAUGH = re.compile(
    r"(?:j+[aeiáé]+){2,}"  # jaja, jajaja, jeje, JAJA
    r"|(?:j+s+){2,}"  # jsjs
    r"|\b(?:x+d+)+\b"  # xd, XDDD, xdxd
    r"|\bl+o+l+\b"  # lol
    r"|\blm+f?a+o+\b"  # lmao, lmaooo, lmfao
    r"|\b(?:h+a+){2,}h?\b"  # haha, hahaha
    r"|\bk{4,}\b"  # kkkk
    r"|\b(?:kekw|lul|omegalul|icant)\b"  # emotes, also inside Kick's [emote:id:NAME]
    r"|[😂🤣💀]",
    re.IGNORECASE,
)


@dataclass
class Spike:
    t: float  # when it happened: a bin start (chat) or a segment start (audio)
    reason: str  # "chat", "laughs" or "loud"
    strength: float  # times the baseline rate, the laughing share, or LU above the median


class ChatSignal:
    """Messages counted in BIN_S bins. `check(now)` judges the newest full bin against the median bin of
    the BASELINE_S before it: a laughter spike (a laughing share, or many more laughs than usual in a big
    chat that always laughs a little) wins over a plain rate spike. Quiet until MIN_HISTORY_S of
    full bins exist, and for COOLDOWN_S after a spike. Thread-safe: the chat reader adds, the ticker checks."""

    def __init__(self):
        self._bins: dict[int, list[int]] = {}  # bin index -> [messages, laughing messages]
        self._start: float | None = None
        self._judged: int | None = None
        self._last_spike: float | None = None
        self._lock = threading.Lock()

    def add(self, t: float, text: str) -> None:
        laugh = LAUGH.search(text) is not None
        with self._lock:
            if self._start is None or t < self._start:
                self._start = t
            b = self._bins.setdefault(int(t // BIN_S), [0, 0])
            b[0] += 1
            b[1] += laugh

    def check(self, now: float) -> Spike | None:
        with self._lock:
            if self._start is None:
                return None
            idx = int(now // BIN_S) - 1  # the newest full bin
            if self._judged is not None and idx <= self._judged:
                return None
            self._judged = idx
            span = int(BASELINE_S // BIN_S)
            for old in [i for i in self._bins if i < idx - span]:
                del self._bins[old]
            first = math.ceil(self._start / BIN_S)  # the bin the first message fell in may be partial
            lo = max(first, idx - span)
            if idx < first or (idx - lo) * BIN_S < MIN_HISTORY_S:
                return None
            t = idx * BIN_S
            if self._last_spike is not None and t - self._last_spike < COOLDOWN_S:
                return None
            count, laughs = self._bins.get(idx, (0, 0))
            past = [self._bins.get(i, (0, 0)) for i in range(lo, idx)]
            base = statistics.median(b[0] for b in past)
            base_laughs = statistics.median(b[1] for b in past)
            spike = None
            if laughs >= LAUGH_MIN and (laughs >= LAUGH_SHARE * count or laughs >= LAUGH_RATE_X * base_laughs):
                spike = Spike(t, "laughs", round(laughs / count, 3))
            elif count >= CHAT_MIN_MSGS and count >= CHAT_RATE_X * base:
                spike = Spike(t, "chat", round(count / max(base, 1.0), 3))
            if spike is not None:
                self._last_spike = t
            return spike


class AudioSignal:
    """One loudness value per segment. `check()` judges the values added since the last check against the
    median of the BASELINE_S before each, with the same history and cooldown rules as the chat."""

    def __init__(self):
        self._history: deque[tuple[float, float]] = deque()
        self._pending: deque[tuple[float, float]] = deque()
        self._first: float | None = None
        self._last_spike: float | None = None
        self._lock = threading.Lock()

    def add(self, t: float, lufs: float) -> None:
        with self._lock:
            self._pending.append((t, max(lufs, SILENCE_LUFS)))

    def samples(self) -> list[tuple[float, float]]:
        with self._lock:
            return [*self._history, *self._pending]

    def check(self) -> Spike | None:
        with self._lock:
            spike = None
            while self._pending:
                t, v = self._pending.popleft()
                s = self._judge(t, v)
                if spike is None:
                    spike = s
                self._history.append((t, v))
            while self._history and self._history[-1][0] - self._history[0][0] > BASELINE_S:
                self._history.popleft()
            return spike

    def _judge(self, t: float, v: float) -> Spike | None:
        if self._first is None:
            self._first = t
        if t - self._first < MIN_HISTORY_S:
            return None
        if self._last_spike is not None and t - self._last_spike < COOLDOWN_S:
            return None
        past = [x for (u, x) in self._history if t - BASELINE_S <= u < t]
        if not past:
            return None
        above = v - statistics.median(past)
        if above < LOUD_LU:
            return None
        self._last_spike = t
        return Spike(t, "loud", round(above, 2))


_MOMENTARY = re.compile(r"\bM:\s*(-?\d+(?:\.\d+)?)")


def segment_loudness(path: Path) -> float | None:
    """The loudest momentary loudness (LUFS) of a segment's sound; None when it has none or cannot be read."""
    try:
        err = ffmpeg.run(["-nostats", "-i", str(path), "-vn", "-af", "ebur128", "-f", "null", "-"],
                         low_priority=True)
    except (RuntimeError, OSError):
        return None
    values = [float(m) for m in _MOMENTARY.findall(err)]
    return max(values) if values else None


def below_normal_thread() -> None:
    """Lower the calling thread's priority (best effort), so it never competes with a game or a stream."""
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
        elif sys.platform.startswith("linux"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)  # per thread on Linux
    except Exception:
        log.debug("No se pudo bajar la prioridad del hilo", exc_info=True)


def _normal_thread(before: int | None) -> None:
    """Undo below_normal_thread() (best effort); `before` is the Linux nice value it replaced."""
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetThreadPriority(k32.GetCurrentThread(), 0)  # THREAD_PRIORITY_NORMAL
        elif sys.platform.startswith("linux") and before is not None:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), before)
    except Exception:
        log.debug("No se pudo restaurar la prioridad del hilo", exc_info=True)


@contextmanager
def below_normal_scope():
    """The calling thread runs below normal priority inside the block, and at its old priority afterwards."""
    before = None
    if sys.platform.startswith("linux"):
        try:
            before = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
        except Exception:
            pass
    below_normal_thread()
    try:
        yield
    finally:
        _normal_thread(before)


class LoudnessMeter(threading.Thread):
    """Measures each stored segment on its own low-priority thread and feeds the AudioSignal. `put` is the
    capture's `on_segment` hook: it only queues, so the capture thread is never held up."""

    def __init__(self, signal: AudioSignal):
        super().__init__(name="live-loudness", daemon=True)
        self.signal = signal
        self._queue: queue.Queue = queue.Queue(maxsize=METER_QUEUE)
        self._stopping = threading.Event()

    def put(self, path: Path, start: float, duration: float) -> None:
        try:
            self._queue.put_nowait((Path(path), start))
        except queue.Full:
            log.debug("Medidor de volumen atrasado: se salta un trozo")

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass  # the loop sees the flag after the item it is measuring

    def run(self) -> None:
        below_normal_thread()
        while not self._stopping.is_set():
            item = self._queue.get()
            if item is None or self._stopping.is_set():
                return
            path, start = item
            try:
                lufs = segment_loudness(path)
            except Exception:
                log.exception("No se pudo medir el volumen de un trozo")
                continue
            if lufs is not None:
                self.signal.add(start, lufs)
