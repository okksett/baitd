"""Capture thread: follows one live HLS media playlist and fills the ring buffer."""
from __future__ import annotations

import collections
import logging
import shutil
import statistics
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from ..models import UserError
from . import channels
from .buffer import RingBuffer
from .channels import DEFAULT_DELAY_S, Channel, Offline
from .hls import MediaPlaylist, Segment, parse_media_playlist

log = logging.getLogger(__name__)

FIRST_FETCH_S = 60.0
RESOLVE_BACKOFF_S = (5, 15, 30)
MIN_FREE_BYTES = 2 * 1024**3
HTTP_TIMEOUT_S = 15

_TEXTS = {
    "started": "Captura en marcha",
    "reconnecting": "Se cortó el directo; reconectando…",
    "ended": "El directo terminó",
    "disk_full": "Queda poco espacio en el disco → Libera espacio y vuelve a capturar",
    "error": "No se pudo seguir el directo → Revisa tu conexión y vuelve a capturar",
}


class _HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class _Stopped(Exception):
    """stop() was called."""


class _Finished(Exception):
    """The capture is over; state and event are already set."""


_NET_ERRORS = (requests.RequestException, _HttpError)


def _safe(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/…"


class Capture(threading.Thread):
    def __init__(
        self,
        channel: Channel,
        buffer: RingBuffer,
        max_height: int,
        on_event: Callable[[str, dict], None],
        *,
        resolve=channels.stream_url,
        http: requests.Session | None = None,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], bool] | None = None,
    ):
        super().__init__(name=f"capture-{channel.id}", daemon=True)
        self.channel = channel
        self.buffer = buffer
        self.max_height = max_height
        self.on_event = on_event
        self._resolve = resolve
        if http is None:
            http = requests.Session()
            http.headers["User-Agent"] = channels._KICK_HEADERS["User-Agent"]
        self._http = http
        self._clock = clock
        # not `_stop`: threading.Thread uses that name internally
        self._stop_evt = threading.Event()
        self._wait = wait or self._stop_evt.wait
        self.state = "starting"
        self.title = channel.handle
        self.info: dict = {}
        self.has_pdt = False
        self.on_segment: Callable[[Path, float, float], None] | None = None
        self._offsets: collections.deque[float] = collections.deque(maxlen=5)
        self._last_server_time: float | None = None
        self._last_seq: int | None = None
        self._prev: tuple[int, float] | None = None  # (seq, end) of the last segment handled
        self._announced = False
        self._first_poll = True
        self._target = 0.0
        self._inits: dict[str, bytes] = {}  # map url -> init segment bytes

    # ---- public clock helpers -------------------------------------------------

    def offset(self) -> float:
        if self._offsets:
            return float(statistics.median(list(self._offsets)))
        return 0.0

    def server_now(self) -> float:
        return self._clock() + self.offset()

    def latency(self) -> float | None:
        head = self.buffer.head()
        return None if head is None else self.server_now() - head

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_evt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    # ---- thread body ------------------------------------------------------------

    def run(self) -> None:
        try:
            self._run()
        except _Finished:
            pass
        except _Stopped:
            if self.state not in ("ended", "error"):
                self.state = "ended"
        except Exception:
            log.exception("La captura de %s/%s falló", self.channel.platform, self.channel.handle)
            self.state = "error"

    def _notify(self, kind: str, text: str | None = None) -> None:
        try:
            self.on_event(kind, {"text": text or _TEXTS[kind]})
        except Exception:
            log.exception("on_event falló")

    def _finish(self, state: str, kind: str, text: str | None = None) -> None:
        self.state = state
        self._notify(kind, text)
        raise _Finished()

    def _sleep(self, seconds: float) -> None:
        if self._wait(seconds) or self._stop_evt.is_set():
            raise _Stopped()

    def _run(self) -> None:
        try:
            url, info = self._resolve(self.channel, self.max_height)
        except Offline:
            self._finish("ended", "ended")
        except UserError as e:
            self._finish("error", "error", str(e))
        self.info = info or {}
        self.title = str(self.info.get("title") or self.channel.handle)
        log.info("Capturando %s/%s desde %s", self.channel.platform, self.channel.handle, _safe(url))

        pending: MediaPlaylist | None = None
        while True:
            if self._stop_evt.is_set():
                raise _Stopped()
            try:
                playlist = pending or self._fetch_playlist(url)
                pending = None
                self._handle(playlist)
            except _NET_ERRORS as e:
                log.warning("Fallo de red en %s (%s)", _safe(url), type(e).__name__)
                url, pending = self._reconnect()
                continue
            self._sleep(max(1.0, self._target / 2))

    # ---- network ----------------------------------------------------------------

    def _get(self, url: str):
        r = self._http.get(url, timeout=HTTP_TIMEOUT_S)
        if r.status_code >= 400:
            raise _HttpError(r.status_code)
        return r

    def _fetch_playlist(self, url: str) -> MediaPlaylist:
        r = self._get(url)
        got = self._clock()
        pl = parse_media_playlist(r.text, url)
        self._target = pl.target_duration
        date = (getattr(r, "headers", None) or {}).get("Date")
        if date:
            try:
                dt = parsedate_to_datetime(date)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # +0.5 s: the header is truncated to whole seconds
                self._offsets.append(dt.timestamp() + 0.5 - got)
            except (TypeError, ValueError):
                pass
        # some X-SERVER-TIME values are fixed per session: only one that advanced is fresh
        st = pl.server_time
        if st is not None:
            if self._last_server_time is not None and st > self._last_server_time:
                self._offsets.append(st - got)
            self._last_server_time = st
        return pl

    def _reconnect(self) -> tuple[str, MediaPlaylist]:
        """Resolve again (up to three times). Returns the new url and its first playlist."""
        for attempt, backoff in enumerate(RESOLVE_BACKOFF_S):
            self.state = "reconnecting"
            if attempt == 0:
                self._notify("reconnecting")
            self._sleep(backoff)
            try:
                url, info = self._resolve(self.channel, self.max_height)
            except Offline:
                self._finish("ended", "ended")
            except Exception as e:
                log.warning("No se pudo resolver de nuevo (%s)", type(e).__name__)
                continue
            if info:
                self.info = info
            if self._stop_evt.is_set():
                raise _Stopped()
            try:
                return url, self._fetch_playlist(url)
            except _NET_ERRORS as e:
                log.warning("La lista nueva falló en %s (%s)", _safe(url), type(e).__name__)
        self._finish("error", "error")
        raise AssertionError("unreachable")

    # ---- playlist handling ------------------------------------------------------

    def _handle(self, pl: MediaPlaylist) -> None:
        self.has_pdt = any(s.start is not None for s in pl.segments)
        self.state = "capturing"
        if not self._announced:
            self._announced = True
            self._notify("started")

        segs = pl.segments
        if segs and self._last_seq is not None and segs[-1].seq < self._last_seq:
            newest = segs[-1]
            head = self.buffer.head()
            # a restart: the numbering fell by more than the playlist is long, or the newest segment is
            # past everything stored. Anything else is a stale playlist (a lagging CDN node): skip it.
            restarted = (
                self._last_seq - newest.seq > len(segs)
                or (newest.start is not None and head is not None and newest.start >= head)
            )
            if not restarted:
                log.info("Lista atrasada (la numeración bajó un poco); se ignora")
                return
            log.info("La numeración bajó: el directo se reinició")
            self._last_seq = None
            self._prev = None
            self._first_poll = True  # trim this batch to the newest FIRST_FETCH_S again
        if self._first_poll and segs:
            segs = self._newest(segs, FIRST_FETCH_S)
        self._first_poll = False

        fresh = [s for s in segs if self._last_seq is None or s.seq > self._last_seq]
        anchor: float | None = None
        if fresh and self._prev is None and fresh[0].start is None:
            # R5: no program-date-times; the newest segment ends `delay` s behind the server clock
            delay = self.channel.delay_s if self.channel.delay_s is not None else DEFAULT_DELAY_S[self.channel.platform]
            anchor = self.server_now() - delay - sum(s.duration for s in fresh)

        for seg in fresh:
            if self._stop_evt.is_set():
                raise _Stopped()
            start = self._start_of(seg, anchor)
            anchor = None
            if not seg.ad:
                self._store(seg, start)
            self._last_seq = seg.seq if self._last_seq is None else max(self._last_seq, seg.seq)
            self._prev = (seg.seq, start + seg.duration)

        if pl.ended:
            self._finish("ended", "ended")

    @staticmethod
    def _newest(segs: list[Segment], seconds: float) -> list[Segment]:
        kept: list[Segment] = []
        total = 0.0
        for s in reversed(segs):
            if kept and total + s.duration > seconds + 1e-6:
                break
            kept.append(s)
            total += s.duration
        kept.reverse()
        return kept

    def _start_of(self, seg: Segment, anchor: float | None) -> float:
        if seg.start is not None:
            return seg.start
        if anchor is not None:
            return anchor
        assert self._prev is not None
        prev_seq, prev_end = self._prev
        return prev_end + max(0, seg.seq - prev_seq - 1) * seg.duration

    def _init(self, url: str) -> bytes:
        """The init segment of an fMP4 stream, downloaded once per distinct EXT-X-MAP url."""
        data = self._inits.get(url)
        if data is None:
            data = self._get(url).content
            self._inits = {url: data}  # the stream moves on; only the newest init is ever needed again
        return data

    def _store(self, seg: Segment, start: float) -> None:
        try:
            free = shutil.disk_usage(self.buffer.folder).free
        except OSError:
            free = MIN_FREE_BYTES
        if free < MIN_FREE_BYTES:
            self._finish("error", "disk_full")
        ext = "ts"
        data = b""
        if seg.map_url:
            data = self._init(seg.map_url)
            ext = "mp4"
        data += self._get(seg.url).content
        path = self.buffer.add(seg.seq, start, seg.duration, data, ext)
        hook = self.on_segment
        if hook is not None:
            try:
                hook(path, start, seg.duration)
            except Exception:
                log.exception("on_segment falló")
