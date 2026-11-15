"""The live session behind the "En directo" routes: one capture at a time, manual marks, sealing finished
marks into stored moments, and the cached live status of the saved channels.

Nothing here blocks a Flask request on the network or FFmpeg: `start` only starts the capture thread,
a ticker thread closes finished marks, and one worker thread cuts and saves them (and tears a session
down, after its last cut). Each session keeps its segments in its own folder under
`directo/buffer`, so a new capture can start while the previous one is still being sealed.

On a channel that is not the user's, the session also watches the chat and the loudness of each segment
(signals.py) and hands every spike to a Detector, which proposes moments on its own worker (spec §6.4).
"""
from __future__ import annotations

import collections
import logging
import queue
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import config, ffmpeg
from ..clip.pipeline import MIN_CLIP_S
from ..models import UserError
from . import channels, store
from .ai import HourlyLimit
from .buffer import REVIEW_PAD_S, NoFootage, RingBuffer
from .capture import Capture
from .channels import Channel, LiveStatus
from .detector import Detector
from .hotkey import HotkeyError
from .marks import EXTEND_S, MAX_MARK_S, Mark, MarkTracker
from .signals import AudioSignal, ChatSignal, LoudnessMeter

__all__ = ["LiveManager", "StatusBoard", "ACTIVE", "TEXTS"]

log = logging.getLogger(__name__)

TICK_S = 0.5
CLOSE_TIMEOUT_S = 20.0  # quitting waits this long for the last cuts, then cancels them
CANCEL_GRACE_S = 2.0  # and this long for the cancelled work to let go
DETECTOR_JOIN_S = 15.0  # a session's tear-down waits this long for the detector's last saves
EVENTS_KEPT = 30
REFRESH_S = 180.0  # channel statuses are refreshed this often...
IDLE_S = 600.0  # ...while a client asked for them in the last 10 minutes
STATUS_WORKERS = 4
ACTIVE = ("starting", "capturing", "reconnecting")
FINISHED = ("ended", "error")

TEXTS = {
    "no_capture": "No hay ninguna captura activa → Elige un canal y pulsa Capturar",
    "marked": "Momento marcado",
    "extended": f"Momento alargado {EXTEND_S:.0f} s",
    "max_length": f"El momento ya dura el máximo de {MAX_MARK_S / 60:.0f} minutos",
    "too_short": "El momento quedó demasiado corto y no se guardó",
    "no_footage": "No había video guardado para ese momento",
    "saved": "Momento guardado",
    "save_error": "No se pudo guardar el momento → Revisa el espacio en el disco y vuelve a marcarlo",
    "found": "Baitd encontró un momento",
}


def _checked_status(ch: Channel) -> LiveStatus:
    return channels.live_status(ch, raise_errors=True)


class StatusBoard:
    """Cached `LiveStatus` per saved channel. A daemon thread, started by the first `touch`, refreshes them
    every `every_s` while a client asked in the last `idle_s`, with at most `workers` checks at once.
    A failing channel is logged once, and again only after it recovered and failed anew."""

    def __init__(self, status_fn: Callable[[Channel], LiveStatus] | None = None, *, every_s: float = REFRESH_S,
                 idle_s: float = IDLE_S, workers: int = STATUS_WORKERS, clock: Callable[[], float] = time.time):
        self._status_fn = status_fn or _checked_status
        self._every_s = every_s
        self._idle_s = idle_s
        self._workers = workers
        self._clock = clock
        self._statuses: dict[str, LiveStatus] = {}
        self._failing: set[str] = set()
        self._asked: float | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def touch(self) -> None:
        """A client asked for the statuses."""
        with self._lock:
            self._asked = self._clock()
            if self._thread is None and not self._closed.is_set():
                self._thread = threading.Thread(target=self._run, name="live-status", daemon=True)
                self._thread.start()

    def refresh(self) -> None:
        """Check every channel now (in the background)."""
        self.touch()
        self._wake.set()

    def get(self, channel_id: str) -> LiveStatus | None:
        with self._lock:
            return self._statuses.get(channel_id)

    def forget(self, channel_id: str) -> None:
        with self._lock:
            self._statuses.pop(channel_id, None)
            self._failing.discard(channel_id)

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1)  # a daemon: a slow check must not hold up quitting

    def _run(self) -> None:
        while not self._closed.is_set():
            with self._lock:
                asked = self._asked
            if asked is not None and self._clock() - asked <= self._idle_s:
                try:
                    self._round()
                except Exception:
                    log.exception("No se pudo actualizar el estado de los canales")
            self._wake.wait(self._every_s)
            self._wake.clear()

    def _round(self) -> None:
        chs = channels.load_channels()
        todo: queue.SimpleQueue[Channel] = queue.SimpleQueue()
        for ch in chs:
            todo.put(ch)

        def work() -> None:
            while not self._closed.is_set():
                try:
                    ch = todo.get_nowait()
                except queue.Empty:
                    return
                self._check(ch)

        # daemon threads, not an executor: a slow check must never hold up quitting Baitd
        threads = [threading.Thread(target=work, name="live-status-check", daemon=True)
                   for _ in range(min(self._workers, len(chs)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        keep = {c.id for c in chs}
        with self._lock:
            for gone in set(self._statuses) - keep:
                del self._statuses[gone]
            self._failing &= keep

    def _check(self, ch: Channel) -> None:
        try:
            st = self._status_fn(ch)
        except Exception as e:
            with self._lock:
                first = ch.id not in self._failing
                self._failing.add(ch.id)
                self._statuses[ch.id] = LiveStatus(False, checked_at=self._clock())
            if first:  # the text can carry a URL with a token: only its type and the channel
                log.warning("No se pudo comprobar %s/%s (%s)", ch.platform, ch.handle, type(e).__name__)
            return
        with self._lock:
            recovered = ch.id in self._failing
            self._failing.discard(ch.id)
            self._statuses[ch.id] = st
        if recovered:
            log.info("%s/%s vuelve a responder", ch.platform, ch.handle)


@dataclass
class _Session:
    id: str
    channel: Channel
    folder: Path
    buffer: RingBuffer
    tracker: MarkTracker
    capture: Capture | None = None
    inflight: list[Mark] = field(default_factory=list)  # marks handed to the worker, not yet cut
    flushed: bool = False
    # automatic detection, only on a channel that is not the user's
    detector: Detector | None = None
    limit: HourlyLimit | None = None
    chat_signal: ChatSignal | None = None
    audio_signal: AudioSignal | None = None
    meter: LoudnessMeter | None = None
    chat: object | None = None  # the ChatReader once the capture knows the stream; False when it failed to start


def _setting(settings: dict, key: str, ok: Callable[[int], bool]) -> int:
    value = settings.get(key)
    return value if type(value) is int and ok(value) else config.SETTINGS_DEFAULTS[key]


class LiveManager:
    def __init__(self, data_dir: Path, settings: Callable[[], dict], *, capture_factory=Capture,
                 hotkey_factory=None, clock: Callable[[], float] = time.time,
                 status_fn: Callable[[Channel], LiveStatus] | None = None, tick_s: float = TICK_S,
                 chat_factory=None, detector_factory=Detector):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "directo" / "buffer"
        self._settings = settings
        self._capture_factory = capture_factory
        self._hotkey_factory = hotkey_factory  # None (tests): no hotkey is ever registered
        self._chat_factory = chat_factory  # None (tests): no chat is read; Baitd passes chat.make_reader
        self._detector_factory = detector_factory
        self._hotkey = None
        self._hotkey_spec: str | None = None
        self._hotkey_lock = threading.Lock()  # serialises reload and close
        self._clock = clock
        self._tick_s = tick_s
        self.statuses = StatusBoard(status_fn, clock=clock)
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._events: collections.deque[dict] = collections.deque(maxlen=EVENTS_KEPT)
        self._next_id = 1
        self._jobs: queue.Queue = queue.Queue()  # ("seal", session, mark) / ("teardown", session, None)
        self._draining: set[Path] = set()  # folders of ended sessions the worker has not removed yet
        self._worker: threading.Thread | None = None
        self._ticker: threading.Thread | None = None
        self._closed = threading.Event()
        self._cancel = threading.Event()  # set when close() runs out of time: stops a running cut

    # ---- capture ----------------------------------------------------------------

    def start(self, channel_id: str) -> None:
        # under the list's lock: DELETE checks "being captured" under it too, so a channel cannot be
        # removed between this lookup and the capture starting
        with channels._LOCK, self._lock:
            ch = next((c for c in channels.load_channels() if c.id == channel_id), None)
            if ch is None:
                raise UserError("Ese canal ya no está en la lista", "Recarga la página y elige otro canal")
            if self._closed.is_set():
                raise UserError("Baitd se está cerrando", "Vuelve a abrir Baitd para capturar")
            old = self._session
            if old is not None and old.capture.state not in FINISHED:
                raise UserError("Ya hay una captura en marcha", "Detenla antes de empezar otra")
            if old is not None:
                self._end(old)
            self._clear_root()
            s = self._settings()
            before = _setting(s, "live_before_s", lambda v: 10 <= v <= 120)
            after = _setting(s, "live_after_s", lambda v: 5 <= v <= 60)
            max_height = _setting(s, "live_max_height", lambda v: v in (720, 1080))
            sid = secrets.token_hex(6)
            folder = self.root / sid
            session = _Session(sid, ch, folder, RingBuffer(folder / "ts"), MarkTracker(before, after))
            session.capture = self._capture_factory(
                ch, session.buffer, max_height, lambda kind, payload: self._on_capture_event(session, kind, payload))
            if not ch.mine:
                self._watch(session, s)
            self._ensure_threads()
            try:
                session.capture.start()
            except BaseException:
                self._unwatch(session)
                shutil.rmtree(folder, ignore_errors=True)
                raise
            self._session = session
        log.info("Captura iniciada en %s/%s", ch.platform, ch.handle)

    def _watch(self, session: _Session, settings: dict) -> None:
        """Lock held: the signals and the detector of a channel that is not the user's (spec §6.4). The
        capture's segment hook only queues a loudness measurement; the chat starts in `tick`."""
        per_hour = _setting(settings, "live_ai_per_hour", lambda v: 0 <= v <= 30)
        session.limit = HourlyLimit(per_hour, self._clock)
        session.chat_signal, session.audio_signal = ChatSignal(), AudioSignal()
        session.meter = LoudnessMeter(session.audio_signal)
        # the server cannot know which profile the window has selected: the first one decides (ruling)
        profile = (config.list_profiles() or [config.default_profile()])[0]
        session.detector = self._detector_factory(
            session.buffer, session.channel, self._settings, profile,
            save=lambda cut, **kw: self._save_auto(session, cut, **kw), limit=session.limit,
            clock=self._clock, cancel=self._cancel)
        session.capture.on_segment = session.meter.put
        session.meter.start()

    @staticmethod
    def _unwatch(s: _Session) -> None:
        """Stop the chat, the meter and the detector. Each only gets the signal; the tear-down waits."""
        for part in (s.chat, s.meter, s.detector):
            if part:
                try:
                    part.stop()
                except Exception:
                    log.exception("No se pudo detener la detección automática")

    def _start_chat(self, s: _Session) -> None:
        """Lock held. YouTube's chat needs the video id, which the capture knows once it is capturing."""
        video_id = str((s.capture.info or {}).get("id") or "")
        try:
            s.chat = self._chat_factory(s.channel, s.chat_signal.add,
                                        lambda kind, payload: self._on_capture_event(s, kind, payload),
                                        video_id=video_id)
            s.chat.start()
        except Exception:
            log.exception("No se pudo abrir el chat de %s/%s", s.channel.platform, s.channel.handle)
            s.chat = False  # not again: the audio alone goes on

    def _detect(self, s: _Session, head: float | None) -> None:
        """Lock held: pass the signals' spikes to the detector and let it take the windows that ended."""
        if s.detector is None or head is None or s.channel.mine:
            return
        if s.chat is None and self._chat_factory is not None and s.capture.state == "capturing":
            self._start_chat(s)
        now = self._clock()
        sp = s.chat_signal.check(now)
        if sp is not None:  # chat bins use this PC's clock; the newest footage is "now" on the stream
            sp.t += head - now
            s.detector.spike(sp)
        sp = s.audio_signal.check()
        if sp is not None:
            s.detector.spike(sp)
        s.detector.tick(head)

    def stop(self, wait: bool = True) -> None:
        """Seal what is marked, stop the capture and clear its buffer. With wait=False the cuts and the
        clean-up run on the worker thread and this returns at once (the route does that)."""
        with self._lock:
            s = self._session
            if s is not None:
                self._end(s)
        if wait and not self._wait_jobs(CLOSE_TIMEOUT_S):
            log.warning("Los últimos momentos de la captura siguen guardándose")

    def _wait_jobs(self, timeout: float) -> bool:
        """Wait until the worker has nothing left, at most `timeout` s. False when time ran out."""
        deadline = time.monotonic() + timeout
        while self._jobs.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def _end(self, s: _Session) -> None:
        """Lock held: detach the session, then queue its marks' cuts and its tear-down, in that order."""
        self._session = None
        s.capture.stop(timeout=0)  # only signals; the tear-down job waits for the thread
        self._unwatch(s)
        for m in s.tracker.flush():
            self._enqueue_seal(s, m)
        self._draining.add(s.folder)
        self._jobs.put(("teardown", s, None))

    def _clear_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for child in self.root.iterdir():
            if child in self._draining:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    def capturing_channel_id(self) -> str | None:
        """The channel being captured right now, if any."""
        with self._lock:
            s = self._session
            return s.channel.id if s is not None and s.capture.state in ACTIVE else None

    def _on_capture_event(self, s: _Session, kind: str, payload: dict) -> None:
        with self._lock:
            if self._session is not s:  # a stopped capture's last words
                return
            self._event(kind, str((payload or {}).get("text") or ""))

    # ---- marks --------------------------------------------------------------------

    def mark(self, origin: str = "button") -> dict:
        with self._lock:
            s = self._session
            if s is None or s.capture.state not in ACTIVE:
                return self._event("no_capture")
            head = s.buffer.head()
            # R5: on the user's own channel "now" is the server clock; on others it is the newest footage
            t = s.capture.server_now() if s.channel.mine or head is None else head
            before = s.tracker.pending()
            m = s.tracker.press(t)
            s.tracker.clamp_start(s.buffer.tail())
            self._update_pin(s)
            if m.presses == 1:
                kind = "marked"
            elif before is not None and m.end == before.end:
                kind = "max_length"
            else:
                kind = "extended"
            log.info("Marca (%s) en %s/%s", origin, s.channel.platform, s.channel.handle)
            return self._event(kind)

    def tick(self) -> None:
        """Hand finished marks to the worker; seal everything when the stream is over."""
        with self._lock:
            s = self._session
            if s is None:
                return
            if s.capture.state in FINISHED:
                if not s.flushed:
                    s.flushed = True
                    for m in s.tracker.flush():
                        self._enqueue_seal(s, m)
                    if s.detector is not None:
                        s.detector.flush()
                    self._update_pin(s)
                return
            head = s.buffer.head()
            while (m := s.tracker.ready(head)) is not None:
                self._enqueue_seal(s, m)
            self._detect(s, head)
            self._update_pin(s)

    def _enqueue_seal(self, s: _Session, m: Mark) -> None:
        s.inflight.append(m)
        self._jobs.put(("seal", s, m))

    @staticmethod
    def _update_pin(s: _Session) -> None:
        """Keep the footage of open and queued marks and of the detector's windows (plus the review padding)
        out of pruning."""
        starts = [m.start for m in s.inflight]
        for earliest in (s.tracker.earliest_start(), s.detector.earliest() if s.detector is not None else None):
            if earliest is not None:
                starts.append(earliest)
        s.buffer.pin(min(starts) - REVIEW_PAD_S if starts else None)

    # ---- worker -------------------------------------------------------------------

    def _ensure_threads(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="live-seal", daemon=True)
            self._worker.start()
        if self._ticker is None:
            self._ticker = threading.Thread(target=self._tick_loop, name="live-tick", daemon=True)
            self._ticker.start()

    def _tick_loop(self) -> None:
        while not self._closed.wait(self._tick_s):
            try:
                self.tick()
            except Exception:
                log.exception("El reloj de la captura falló")

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                kind, s, m = job
                if kind == "seal":
                    self._seal(s, m)
                else:
                    self._teardown(s)
            except Exception:
                log.exception("Un trabajo de la captura falló")
            finally:
                self._jobs.task_done()

    def _display_name(self, s: _Session) -> str:
        name = str((s.capture.info or {}).get("uploader") or "") if s.capture is not None else ""
        if not name:
            st = self.statuses.get(s.channel.id)
            name = st.name if st is not None else ""
        return name or s.channel.handle.lstrip("@")

    def _seal(self, s: _Session, m: Mark) -> None:
        if self._cancel.is_set():  # Baitd is quitting and out of time: no new cuts
            log.warning("Un momento del directo no se guardó: Baitd se cerró antes")
            return
        out = s.folder / "cortes" / f"{secrets.token_hex(6)}.mp4"
        try:
            try:
                cut = s.buffer.cut(m.start, m.end, out, pad=REVIEW_PAD_S, cancel=self._cancel)
            except NoFootage:
                self._event("no_footage")
                return
            if cut.end - cut.start < MIN_CLIP_S:
                self._event("too_short")
                return
            at = max(m.start, m.end - s.tracker.after_s)  # the last press
            name = self._display_name(s)
            store.save_moment(cut, channel=s.channel, title=f"{name} · {datetime.fromtimestamp(at):%H:%M}",
                              reason="manual", at=at, session_id=s.id, framing=s.channel.framing,
                              display_name=name)
            self._event("saved")
        except ffmpeg.JobCancelled:
            log.warning("Un momento del directo no se guardó: Baitd se cerró antes")
        except Exception:
            log.exception("No se pudo guardar un momento del directo")
            self._event("save_error")
        finally:
            out.unlink(missing_ok=True)  # save_moment moved it; anything left is a failed cut
            with self._lock:
                if m in s.inflight:
                    s.inflight.remove(m)
                if self._session is s:
                    self._update_pin(s)

    def _save_auto(self, s: _Session, cut, *, reason: str, at: float, title: str, kind: str, classified: bool,
                   description: str, hashtags: list[str], text: str) -> None:
        """A moment from the detector (on its worker). An unclassified one is named like a manual mark."""
        try:
            name = self._display_name(s)
            store.save_moment(cut, channel=s.channel, title=title or f"{name} · {datetime.fromtimestamp(at):%H:%M}",
                              reason=reason, at=at, session_id=s.id, framing=s.channel.framing, kind=kind,
                              classified=classified, description=description, hashtags=hashtags, text=text,
                              display_name=name)
        except Exception:
            log.exception("No se pudo guardar un momento automático")
            self._event("save_error")
            return
        log.info("Momento automático (%s, %s) en %s/%s", reason, kind, s.channel.platform, s.channel.handle)
        self._event("saved", TEXTS["found"])

    def _teardown(self, s: _Session) -> None:
        try:
            # waits for the capture thread, so nothing writes into the folder afterwards (not when quitting late)
            s.capture.stop(timeout=0 if self._cancel.is_set() else 10)
            if s.detector is not None:  # its last saves cut from this buffer
                s.detector.join(0 if self._cancel.is_set() else DETECTOR_JOIN_S)
            for part in (s.meter, s.chat):
                if part:
                    part.join(1)
            s.buffer.clear()
            shutil.rmtree(s.folder, ignore_errors=True)
        finally:
            with self._lock:
                self._draining.discard(s.folder)

    # ---- status -------------------------------------------------------------------

    def _event(self, kind: str, text: str | None = None) -> dict:
        with self._lock:
            ev = {"id": self._next_id, "kind": kind, "text": text if text is not None else TEXTS[kind]}
            self._next_id += 1
            self._events.append(ev)
            return dict(ev)

    def status(self, since: int = 0) -> dict:
        with self._lock:
            events = [dict(e) for e in self._events if e["id"] > since]
            s = self._session
            if s is None:
                return {"state": "idle", "channel_id": None, "title": None, "latency_s": None, "buffered_s": 0,
                        "pending": None, "events": events, "has_pdt": None, "ai_left": None}
            ai_left = s.limit.left() if s.limit is not None else None
            cap = s.capture
            latency = cap.latency() if cap.state in ACTIVE else None
            pending = None
            p = s.tracker.pending()
            if p is not None:
                stream_now = cap.server_now() - (latency or 0.0)
                pending = {"remaining_s": round(max(0.0, p.end - stream_now), 1),
                           "length_s": round(p.end - p.start, 1)}
            return {"state": cap.state, "channel_id": s.channel.id, "title": cap.title,
                    "latency_s": round(latency, 1) if latency is not None else None,
                    "buffered_s": round(s.buffer.seconds(), 1), "pending": pending, "events": events,
                    "has_pdt": bool(cap.has_pdt), "ai_left": ai_left}

    # ---- channels and lifetime ----------------------------------------------------

    def channels_changed(self) -> None:
        """The saved channels changed. The session sees its channel's new settings (framing, mine, delay);
        the hotkey is registered or unregistered here under R4."""
        with self._lock:
            s = self._session
        if s is not None:
            fresh = next((c for c in channels.load_channels() if c.id == s.channel.id), None)
            if fresh is not None:
                with self._lock:  # in place: the capture thread holds the same object
                    s.channel.mine, s.channel.framing, s.channel.delay_s = fresh.mine, fresh.framing, fresh.delay_s
        self.reload_hotkey()

    # ---- hotkey (R4) ----------------------------------------------------------------

    def reload_hotkey(self) -> None:
        """Make the registered hotkey match the saved channels and the `live_hotkey` setting: registered while
        Baitd runs and at least one channel is saved, released when the last channel is removed. A refused
        key becomes the `hotkey_busy` event (and is tried again on the next reload)."""
        if self._hotkey_factory is None:
            return
        with self._hotkey_lock:
            if self._closed.is_set():
                return
            spec = str(self._settings().get("live_hotkey") or config.SETTINGS_DEFAULTS["live_hotkey"])
            want = spec if channels.load_channels() else None
            if self._hotkey is not None and want == self._hotkey_spec:
                return
            old, self._hotkey, self._hotkey_spec = self._hotkey, None, None
            if old is not None:
                self._stop_hotkey(old)
            if want is None:
                return
            try:
                listener = self._hotkey_factory(want, self._on_hotkey)
                listener.start()
            except HotkeyError as e:
                self._event("hotkey_busy", str(e))
                return
            except Exception:
                log.exception("No se pudo activar la tecla rápida")
                self._event("hotkey_busy", str(HotkeyError("No se pudo activar la tecla rápida",
                                                           "Usa el botón Marcar de la ventana")))
                return
            self._hotkey, self._hotkey_spec = listener, want

    @staticmethod
    def _stop_hotkey(listener) -> None:
        try:
            listener.stop()
        except Exception:
            log.exception("No se pudo soltar la tecla rápida")

    def _on_hotkey(self) -> None:
        """The key was pressed (on the listener's thread): `mark` is quick, so the message loop stays free."""
        try:
            self.mark("hotkey")
        except Exception:
            log.exception("No se pudo marcar el momento con la tecla rápida")

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> None:
        """App exit: seal and stop the capture, then unregister the hotkey and end the threads.

        The last cuts get `timeout` s; then a running cut is cancelled, so quitting never hangs on FFmpeg."""
        if self._closed.is_set():
            return
        self._closed.set()  # no new capture from here on; the ticker stops
        self.stop(wait=False)
        drained = self._wait_jobs(timeout)
        if not drained:
            log.warning("La captura no terminó de guardar en %.0f s: se cancela lo pendiente", timeout)
            self._cancel.set()
            drained = self._wait_jobs(CANCEL_GRACE_S)
        with self._hotkey_lock:
            hotkey, self._hotkey, self._hotkey_spec = self._hotkey, None, None
        if hotkey is not None:
            self._stop_hotkey(hotkey)
        self.statuses.close()
        if self._worker is not None:
            self._jobs.put(None)
            self._worker.join(5 if drained else 0.5)  # a daemon: a stuck job does not keep Baitd open
        if self._ticker is not None:
            self._ticker.join(2)
