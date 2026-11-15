"""Automatic moments on someone else's channel (spec §6.4, stages 2 and 3).

A spike from the chat or the audio opens a candidate window around it; overlapping windows merge. Once the
buffer holds the whole window, one below-normal-priority worker transcribes just that window and asks the
AI once whether it is humour, controversy or nothing, and which span to keep. "Nothing" is dropped. Without
words, without a request left this hour, or without quota, the window is saved unclassified with the
signal's reason, and the AI is asked in review.
"""
from __future__ import annotations

import logging
import math
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import ffmpeg, llm
from ..clip import transcribe as clip_transcribe
from ..clip.moments import _sentences
from ..clip.pipeline import MIN_CLIP_S
from ..llm._http import QuotaExhausted
from ..models import Profile, UserError, Word
from . import ai
from .ai import HourlyLimit
from .buffer import BUFFER_S, NoFootage, RingBuffer
from .channels import Channel
from .signals import Spike, below_normal_thread

__all__ = ["CANDIDATE_BEFORE_S", "CANDIDATE_AFTER_S", "Detector"]

log = logging.getLogger(__name__)

CANDIDATE_BEFORE_S = 40.0
CANDIDATE_AFTER_S = 20.0
MAX_CANDIDATE_S = 120.0  # merging stops here; a later spike starts a new window where this one ends
MAX_WAITING = 3  # at most this many windows wait; a newer one pushes out the oldest
CPU_THREADS = 2  # the local transcriber's threads while a stream may be running on this PC
_PRIORITY = {"laughs": 0, "chat": 1, "loud": 2}  # a merged window keeps the most telling reason


@dataclass
class _Candidate:
    start: float
    end: float
    reason: str
    at: float  # the first spike


class Detector:
    """`spike` and `tick` are quick (the manager's ticker calls them); the work runs on one worker thread,
    started with the first ready window. `save(cut, *, reason, at, title, kind, classified, description,
    hashtags, text)` stores a moment; an empty title means "name it like a manual mark"."""

    def __init__(self, buffer: RingBuffer, channel: Channel, settings: Callable[[], dict], profile: Profile, *,
                 save: Callable[..., None], transcribe=None, complete: Callable[[str], str] | None = None,
                 limit: HourlyLimit, clock: Callable[[], float] = time.time,
                 cancel: threading.Event | None = None):
        self._buffer = buffer
        self.channel = channel
        self._settings = settings
        self._profile = profile
        self._save = save
        self._transcribe = transcribe or clip_transcribe.transcribe
        self._complete = complete or (lambda prompt: llm.complete(prompt, profile, settings(), cancel=self._busy))
        self._limit = limit
        self._clock = clock
        self._busy = threading.Event()  # set by stop(): cancels a transcription or an AI request under way
        self._abort = cancel or threading.Event()  # set when Baitd is quitting out of time: cancels the cuts too
        self._work_dir = Path(buffer.folder).parent
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._open: list[_Candidate] = []
        self._waiting: deque[_Candidate] = deque()
        self._running: _Candidate | None = None
        self._sent_end = -math.inf  # the end of the last window handed to the worker
        self._head: float | None = None
        self._stopping = False
        self._thread: threading.Thread | None = None

    # ---- called by the manager --------------------------------------------------

    def spike(self, s: Spike) -> None:
        """Open a window around the spike, or widen the open one it overlaps."""
        with self._lock:
            if self._stopping:
                return
            start = max(s.t - CANDIDATE_BEFORE_S, self._sent_end)  # never what the worker already has
            end = s.t + CANDIDATE_AFTER_S
            if end - start < MIN_CLIP_S:
                return
            for c in self._open:
                if start <= c.end and end >= c.start:
                    if max(end, c.end) - min(start, c.start) <= MAX_CANDIDATE_S:
                        c.start, c.end = min(start, c.start), max(end, c.end)
                        if _PRIORITY[s.reason] < _PRIORITY[c.reason]:
                            c.reason = s.reason
                        return
                    start = max(start, c.end)
            if end - start < MIN_CLIP_S:
                return
            self._open.append(_Candidate(start, end, s.reason, s.t))
            self._open.sort(key=lambda c: c.start)

    def tick(self, head: float | None) -> None:
        """Hand the windows the buffer now holds whole (end <= head) to the worker."""
        with self._lock:
            if head is None or self._stopping:
                return
            self._head = head
            for c in [c for c in self._open if c.end <= head]:
                self._open.remove(c)
                self._send(c)
            self._trim()

    def flush(self) -> None:
        """The stream ended: the open windows end at the last footage and go to the worker now."""
        with self._lock:
            if not self._stopping:
                self._send_open()
                self._trim()

    def stop(self) -> None:
        """No more transcriptions or AI requests: what is open or waiting is saved unclassified, then the worker
        ends. A transcription under way is cancelled (and its window saved unclassified); an AI request under
        way is not interrupted, only its retry waits are. Returns at once; `join` waits."""
        with self._lock:
            if self._stopping:
                return
            self._send_open()
            self._stopping = True
            self._busy.set()
            self._wake.notify_all()

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def idle(self) -> bool:
        """No window waiting or being handled."""
        with self._lock:
            return not self._waiting and self._running is None

    def earliest(self) -> float | None:
        """The earliest footage a window still needs (the manager pins the buffer there, minus the padding).
        Never more than BUFFER_S + MAX_CANDIDATE_S behind the head: a window stuck in a slow transcription or
        request must not keep the disk filling. Its footage may be pruned meanwhile; it is then dropped, or
        saved with what is left."""
        with self._lock:
            starts = [c.start for c in (*self._open, *self._waiting)]
            if self._running is not None:
                starts.append(self._running.start)
            if not starts:
                return None
            if self._head is None:
                return min(starts)
            return max(min(starts), self._head - BUFFER_S - MAX_CANDIDATE_S)

    # ---- worker -------------------------------------------------------------------

    def _send_open(self) -> None:
        """Lock held."""
        for c in self._open:
            if self._head is not None:
                c.end = min(c.end, self._head)
            self._send(c)
        self._open.clear()

    def _trim(self) -> None:
        """Lock held: keep the newest MAX_WAITING windows waiting."""
        while len(self._waiting) > MAX_WAITING:
            old = self._waiting.popleft()
            log.warning("Demasiados candidatos esperando: se descarta uno (%s)", old.reason)

    def _send(self, c: _Candidate) -> None:
        """Lock held."""
        self._waiting.append(c)
        self._sent_end = max(self._sent_end, c.end)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="live-detect", daemon=True)
            self._thread.start()
        self._wake.notify_all()

    def _run(self) -> None:
        below_normal_thread()
        with ffmpeg.low_priority():
            while True:
                with self._lock:
                    while not self._waiting and not self._stopping:
                        self._wake.wait()
                    if not self._waiting or self._abort.is_set():
                        self._waiting.clear()
                        return
                    c = self._running = self._waiting.popleft()
                    quick = self._stopping
                began = self._clock()
                try:
                    self._handle(c, quick)
                except ffmpeg.JobCancelled:
                    log.warning("Un momento automático no se guardó: Baitd se cerró antes")
                except Exception:
                    log.exception("No se pudo revisar un momento automático")
                finally:
                    with self._lock:
                        self._running = None
                log.info("Candidato (%s) revisado en %.0f s", c.reason, self._clock() - began)

    def _handle(self, c: _Candidate, quick: bool) -> None:
        words: list[Word] = []
        language = ""
        if not quick:
            try:
                words, language = self._listen(c)
            except NoFootage:
                log.info("Candidato sin video en el búfer: se descarta")
                return
            except ffmpeg.JobCancelled:
                if self._abort.is_set():
                    raise
            except UserError as e:  # no speech, no sound, or the transcriber is unavailable
                log.info("Candidato sin transcripción: %s", e.cause)
            except RuntimeError:
                log.warning("No se pudo leer el audio de un candidato")
        lines = ai.window_lines(words, c.start, c.end) if words else ""
        if lines and not self._busy.is_set() and self._limit.take():
            verdict = self._ask(lines, language, c)
            if verdict is not None and verdict.kind == "nada":
                log.info("La IA descartó un candidato (%s)", c.reason)
                return
            if verdict is not None:
                end = _line_end(words, verdict.start, verdict.end, c.end)
                self._store(c, verdict.start, end, words, kind=verdict.kind, classified=True,
                            title=verdict.title, description=verdict.description, hashtags=verdict.hashtags)
                return
        self._store(c, c.start, c.end, words, kind="interesante", classified=False, title="",
                    description="", hashtags=[])

    def _listen(self, c: _Candidate) -> tuple[list[Word], str]:
        """The window's words on the buffer's clock, and the language to ask in."""
        opts = self._profile.clipping
        language = None if opts.language == "auto" else opts.language
        with tempfile.TemporaryDirectory(prefix="detect-", dir=self._work_dir) as tmp:
            cut = self._buffer.cut(c.start, c.end, Path(tmp) / "window.mp4", pad=0, cancel=self._busy)
            wav = Path(tmp) / "audio.wav"
            # what sources.extract_audio does, for a file that is not a stored source
            ffmpeg.run(["-i", str(cut.path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "pcm_s16le", str(wav)], cancel=self._busy)
            t = self._transcribe(wav, opts.transcriber, language, self._settings(), opts.whisper_model,
                                 self._busy, lambda f: None, cpu_threads=CPU_THREADS)
        # through the joined segments: a dropped ad or a lost segment inside the window shifts what follows it
        words = [Word(w.text, cut.buffer_time(w.start), cut.buffer_time(w.end)) for w in t.words]
        return words, t.language or language or "the transcript's own"

    def _ask(self, lines: str, language: str, c: _Candidate) -> ai.AiVerdict | None:
        try:
            raw = self._complete(ai.build_prompt(lines, language, want_span=True))
        except QuotaExhausted:
            log.warning("Se acabó la cuota de la IA: los momentos automáticos quedan sin clasificar")
            self._limit.exhaust()
            return None
        except ffmpeg.JobCancelled:
            if self._abort.is_set():
                raise
            return None
        except UserError as e:
            log.warning("La IA no respondió por un candidato: %s", e.cause)
            return None
        verdict = ai.parse_reply(raw, c.start, c.end, want_span=True)
        if verdict is None:
            log.warning("La IA devolvió una respuesta ilegible por un candidato")
        return verdict

    def _store(self, c: _Candidate, t0: float, t1: float, words: list[Word], **fields) -> None:
        out = self._work_dir / "cortes" / f"{secrets.token_hex(6)}.mp4"
        try:
            try:
                cut = self._buffer.cut(t0, t1, out, cancel=self._abort)  # with the review padding
            except NoFootage:
                log.info("Candidato sin video en el búfer: se descarta")
                return
            if cut.end - cut.start < MIN_CLIP_S:
                log.info("Candidato demasiado corto: se descarta")
                return
            text = " ".join(w.text for w in words if t0 <= w.start < t1)
            self._save(cut, reason=c.reason, at=c.at, text=text, **fields)
        finally:
            out.unlink(missing_ok=True)  # save moved it; anything left is a failed cut


def _line_end(words: list[Word], start: float, end: float, limit: float) -> float:
    """`fin` is the mark of the last line to keep (its start, in whole seconds): the span runs to the end of
    that line, at most to `limit`."""
    last = end
    for s0, s1, _i0, _i1 in _sentences(words):
        if start <= s0 < end + 1.0:
            last = max(last, s1)
    return min(last, limit)
