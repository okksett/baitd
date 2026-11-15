"""Anonymous live-chat readers (Twitch IRC, Kick Centrifugo, YouTube live_chat).

Only the receive time and the text of each message are kept: a later stage looks for moments when the
chat explodes or laughs. Nothing here signs in, and nothing sensitive (tokens, continuations, query URLs)
is ever logged: only the platform, the handle and the exception type.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import threading
import time
import uuid
from typing import Awaitable, Callable

import aiohttp

from .channels import _KICK_HEADERS, Channel

log = logging.getLogger("baitd.live.chat")

MAX_FAILS = 5
KICK_TOKEN_REFRESH_S = 40 * 60       # the guest token lasts ~45 min
_NAMES = {"twitch": "Twitch", "kick": "Kick", "youtube": "YouTube"}
_BROWSER_UA = {"User-Agent": "Mozilla/5.0"}
# YouTube refuses the live_chat page ("older version of your browser") unless the User-Agent looks current
_YT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/130.0.0.0 Safari/537.36")
_KICK_AUTH = "https://web.kick.com/api/v1/realtime/auth/connection"
_KICK_WS = "wss://realtime.us-east-1.platform.kick.com/connection/websocket"
_YT_CHAT = "https://www.youtube.com/live_chat?is_popout=1&v={v}"
_YT_POLL = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat"
_YT_DEFAULT_POLL_MS = 5000


# ---------- pure parsers ----------

def twitch_privmsgs(raw: str) -> tuple[list[str], bool]:
    """Message texts in an IRC chunk, and whether a PING must be answered."""
    texts: list[str] = []
    ping = False
    for line in raw.split("\n"):
        line = line.strip("\r")
        if line.startswith("PING"):
            ping = True
            continue
        m = re.search(r"\sPRIVMSG #\S+ :(.*)$", line)
        if m:
            texts.append(m.group(1))
    return texts, ping


def kick_text(frame: dict) -> str | None:
    """The text of a Kick chat push frame, or None for anything else."""
    try:
        data = frame["push"]["pub"]["data"]
        if isinstance(data, str):
            data = json.loads(data)
        if data.get("event") != "App\\Events\\ChatMessageEvent":
            return None
        inner = data["data"]
        if isinstance(inner, str):
            inner = json.loads(inner)
        text = inner.get("content")
        return text if isinstance(text, str) and text else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def youtube_parse(data: dict) -> tuple[list[str], str | None, int]:
    """(texts, next continuation, poll delay in ms) from a get_live_chat reply."""
    cont = ((data.get("continuationContents") or {}).get("liveChatContinuation")) or {}
    texts: list[str] = []
    for act in cont.get("actions") or []:
        item = ((act.get("addChatItemAction") or {}).get("item")) or {}
        r = item.get("liveChatTextMessageRenderer")
        if not r:
            continue
        parts = []
        for run in (r.get("message") or {}).get("runs") or []:
            if "text" in run:
                parts.append(run["text"])
            elif "emoji" in run:
                sc = run["emoji"].get("shortcuts") or []
                if sc:
                    parts.append(sc[0])
        if parts:
            texts.append("".join(parts))
    nxt, delay = None, _YT_DEFAULT_POLL_MS
    for c in cont.get("continuations") or []:
        for key in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            d = c.get(key)
            if d and d.get("continuation"):
                nxt = d["continuation"]
                delay = int(d.get("timeoutMs") or _YT_DEFAULT_POLL_MS)
                break
        if nxt:
            break
    return texts, nxt, delay


# ---------- reader ----------

class ChatReader(threading.Thread):
    def __init__(self, channel: Channel, on_message: Callable[[float, str], None], on_event, *,
                 video_id: str = "", connect: Callable[["ChatReader"], Awaitable[None]] | None = None,
                 retry_wait: float | None = None):
        super().__init__(name=f"chat-{channel.platform}", daemon=True)
        self.channel = channel
        self.on_message = on_message
        self.on_event = on_event
        self.video_id = video_id
        self._connect = connect or {"twitch": _twitch, "kick": _kick, "youtube": _youtube}.get(channel.platform)
        self._retry_wait = retry_wait
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self.healthy = False     # set by a session once it really received something

    def emit(self, text: str) -> None:
        """Called by the sessions for every chat message."""
        self.healthy = True
        try:
            self.on_message(time.time(), text)
        except Exception:
            log.exception("on_message falló")

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            with self._lock:
                self._loop = loop
                if not self._stopping.is_set():
                    self._task = loop.create_task(self._main())
                task = self._task
            if task is not None:
                try:
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    pass
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    async def _main(self) -> None:
        fails = 0
        plat = self.channel.platform
        while not self._stopping.is_set():
            self.healthy = False
            try:
                await self._connect(self)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("chat %s/%s: %s", plat, self.channel.handle, type(e).__name__)
            fails = 0 if self.healthy else fails + 1
            if fails >= MAX_FAILS:
                name = _NAMES.get(plat, plat)
                try:
                    self.on_event("chat_off", {"text": f"No se pudo leer el chat de {name} → Baitd sigue solo con el audio"})
                except Exception:
                    log.exception("on_event falló")
                return
            wait = self._retry_wait if self._retry_wait is not None else min(2.0 * max(fails, 1), 15.0)
            await asyncio.sleep(wait)


def make_reader(channel: Channel, on_message, on_event, video_id: str = "") -> ChatReader:
    return ChatReader(channel, on_message, on_event, video_id=video_id)


# ---------- platform sessions ----------

async def _twitch(r: ChatReader) -> None:
    login = r.channel.handle.lower()
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("wss://irc-ws.chat.twitch.tv:443", heartbeat=None) as ws:
            await ws.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send_str("PASS SCHMOOPIIE")
            await ws.send_str(f"NICK justinfan{random.randint(10000, 99999)}")
            await ws.send_str(f"JOIN #{login}")
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                if " JOIN " in msg.data or " 366 " in msg.data:
                    r.healthy = True
                texts, ping = twitch_privmsgs(msg.data)
                if ping:
                    await ws.send_str("PONG :tmi.twitch.tv")
                for t in texts:
                    r.emit(t)


async def _kick(r: ChatReader) -> None:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://kick.com/api/v2/channels/{r.channel.handle}", headers=_KICK_HEADERS,
                         timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            room = (await resp.json(content_type=None))["chatroom"]["id"]
        async with s.post(_KICK_AUTH, json={"client_id": str(uuid.uuid4())}, headers=_BROWSER_UA,
                          timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            token = (await resp.json(content_type=None))["data"]["token"]
        started = time.monotonic()
        async with s.ws_connect(_KICK_WS, headers={"Origin": "https://kick.com", **_BROWSER_UA}, heartbeat=None) as ws:
            await ws.send_str(json.dumps({"id": 1, "connect": {"token": token, "name": "js"}}))
            await ws.send_str(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{room}.v2"}}))
            while True:
                left = KICK_TOKEN_REFRESH_S - (time.monotonic() - started)
                if left <= 0:
                    return                      # reconnect with a fresh token
                try:
                    msg = await ws.receive(timeout=left)
                except asyncio.TimeoutError:
                    return
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        return
                    continue
                for line in msg.data.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if frame == {}:
                        await ws.send_str("{}")
                        continue
                    if "connect" in frame or "subscribe" in frame:
                        r.healthy = True
                        continue
                    text = kick_text(frame)
                    if text:
                        r.emit(text)


async def _youtube(r: ChatReader) -> None:
    if not r.video_id:
        raise ValueError("sin id de vídeo")
    async with aiohttp.ClientSession(headers={"User-Agent": _YT_UA, "Accept-Language": "en-US,en;q=0.9"}) as s:
        async with s.get(_YT_CHAT.format(v=r.video_id), timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            page = await resp.text()
        mv = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', page)
        mc = re.search(r'"continuation"\s*:\s*"([^"]+)"', page)
        if not mv or not mc:
            raise ValueError("chat no disponible")
        version, cont = mv.group(1), mc.group(1)
        while cont:
            body = {"context": {"client": {"clientName": "WEB", "clientVersion": version}}, "continuation": cont}
            async with s.post(_YT_POLL, json=body, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            texts, cont, delay = youtube_parse(data)
            r.healthy = True
            for t in texts:
                r.emit(t)
            await asyncio.sleep(max(delay, 1000) / 1000)
