"""Saved live channels (Kick, Twitch, YouTube): parsing a pasted link, the list on disk, whether a channel is
live now, and the HLS media playlist to capture. Anonymous viewer only: no accounts, no sign-in."""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import requests

from .. import config
from ..clip import sources, ytdlp_loader
from ..models import UserError

__all__ = ["PLATFORMS", "DEFAULT_DELAY_S", "MAX_CHANNELS", "FRAMINGS", "Channel", "LiveStatus", "Offline",
           "parse_channel", "load_channels", "save_channels", "add_channel", "live_status", "pick_format",
           "stream_url"]

log = logging.getLogger(__name__)

PLATFORMS = ("kick", "twitch", "youtube")
DEFAULT_DELAY_S = {"kick": 8.0, "twitch": 6.0, "youtube": 15.0}
MAX_CHANNELS = 50
FRAMINGS = ("cara", "partido")
FILE = "canales.json"
_LOCK = threading.RLock()

_KICK = re.compile(r"[a-z0-9_-]{2,40}")
_TWITCH = re.compile(r"[a-z0-9_]{3,25}")
_YT_HANDLE = re.compile(r"@[\w.-]{3,40}")
_YT_ID = re.compile(r"UC[\w-]{22}")
_KICK_RESERVED = {"video", "videos", "categories", "category", "browse", "search", "dashboard", "clips"}
_TWITCH_RESERVED = {"videos", "directory", "downloads", "settings", "search", "p", "jobs", "turbo", "store",
                    "subscriptions", "wallet", "friends", "inventory", "drops", "moderator", "popout"}
_OFFLINE_MARKS = ("not currently live", "is offline", "usernotlive")
_KICK_HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}


class Offline(Exception):
    """The channel is not live right now."""


@dataclass
class Channel:
    id: str
    platform: str
    handle: str
    url: str
    mine: bool = False
    framing: str = "cara"
    delay_s: float | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "platform": self.platform, "handle": self.handle, "url": self.url,
                "mine": self.mine, "framing": self.framing, "delay_s": self.delay_s}

    @classmethod
    def from_dict(cls, d: dict) -> "Channel":
        framing = d.get("framing")
        delay = d.get("delay_s")
        ok_delay = type(delay) in (int, float) and 0 <= delay <= 60
        return cls(id=str(d["id"]), platform=str(d["platform"]), handle=str(d["handle"]), url=str(d["url"]),
                   mine=d.get("mine") is True, framing=framing if framing in FRAMINGS else "cara",
                   delay_s=float(delay) if ok_delay else None)


@dataclass
class LiveStatus:
    live: bool
    title: str = ""
    viewers: int | None = None
    name: str = ""
    checked_at: float = 0.0


def _bad_link() -> UserError:
    return UserError("Ese enlace no es de un canal de Kick, Twitch o YouTube",
                     "Pega el enlace del canal, por ejemplo kick.com/tu_canal")


def parse_channel(text: str) -> tuple[str, str, str]:
    """(platform, handle, canonical url) from a pasted link; UserError when it is not a channel link."""
    try:
        u = urlparse(sources.normalize_url(text))
        host = (u.hostname or "").lower()
    except ValueError:
        raise _bad_link() from None
    if u.scheme not in ("http", "https"):
        raise _bad_link()
    parts = [p for p in u.path.split("/") if p]

    def on(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if parts:
        first = parts[0]
        if on("kick.com"):
            slug = first.lower()
            if _KICK.fullmatch(slug) and slug not in _KICK_RESERVED:
                return "kick", slug, f"https://kick.com/{slug}"
        elif on("twitch.tv"):
            login = first.lower()
            if _TWITCH.fullmatch(login) and login not in _TWITCH_RESERVED:
                return "twitch", login, f"https://www.twitch.tv/{login}"
        elif on("youtube.com"):
            if _YT_HANDLE.fullmatch(first):
                return "youtube", first, f"https://www.youtube.com/{first}/live"
            if first == "channel" and len(parts) > 1 and _YT_ID.fullmatch(parts[1]):
                return "youtube", parts[1], f"https://www.youtube.com/channel/{parts[1]}/live"
    raise _bad_link()


# ---- the list on disk ----

def _path():
    return config.data_dir() / FILE


def load_channels() -> list[Channel]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            ch = Channel.from_dict(d)
        except (KeyError, TypeError, AttributeError):
            continue
        if ch.platform in PLATFORMS:
            out.append(ch)
    return out


def save_channels(chs: list[Channel]) -> None:
    with _LOCK:
        config._write_json(_path(), [c.to_dict() for c in chs])


def add_channel(text: str, mine: bool) -> Channel:
    platform, handle, url = parse_channel(text)
    with _LOCK:
        chs = load_channels()
        if any(c.platform == platform and c.handle.lower() == handle.lower() for c in chs):
            raise UserError("Ese canal ya está en la lista", "Elige otro canal o quita el que ya tienes")
        if len(chs) >= MAX_CHANNELS:
            raise UserError(f"Ya tienes {MAX_CHANNELS} canales guardados", "Quita alguno para añadir otro")
        ch = Channel(id=secrets.token_hex(6), platform=platform, handle=handle, url=url, mine=bool(mine))
        save_channels(chs + [ch])
    return ch


# ---- live status and the stream ----

def _default_extract(url: str) -> dict:
    ytdlp = ytdlp_loader.import_ytdlp()
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 1,
            **ytdlp_loader.js_runtime_opts()}
    with ytdlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _is_offline_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(m in msg for m in _OFFLINE_MARKS)


def live_status(ch: Channel, *, http=requests, extract: Callable[[str], dict] | None = None) -> LiveStatus:
    now = time.time()
    try:
        if ch.platform == "kick":
            r = http.get(f"https://kick.com/api/v2/channels/{ch.handle}", headers=_KICK_HEADERS, timeout=15)
            r.raise_for_status()
            data = r.json()
            stream = data.get("livestream")
            if not stream:
                return LiveStatus(False, checked_at=now)
            return LiveStatus(True, title=str(stream.get("session_title") or ""),
                              viewers=stream.get("viewer_count"),
                              name=str((data.get("user") or {}).get("username") or ""), checked_at=now)
        try:
            info = (extract or _default_extract)(ch.url)
        except Exception as e:
            if _is_offline_error(e):
                return LiveStatus(False, checked_at=now)
            raise
        if not info or info.get("is_live") is not True:
            return LiveStatus(False, checked_at=now)
        return LiveStatus(True, title=str(info.get("title") or ""), viewers=info.get("concurrent_view_count"),
                          name=str(info.get("uploader") or ""), checked_at=now)
    except Exception as e:
        # The exception text can carry a URL with a token: log only its type and the channel.
        log.warning("No se pudo comprobar %s/%s (%s)", ch.platform, ch.handle, type(e).__name__)
        return LiveStatus(False, checked_at=now)


def pick_format(info: dict, max_height: int) -> dict:
    """The best HLS format with video and audio in one playlist, no taller than max_height."""
    options = [f for f in info.get("formats") or []
               if str(f.get("protocol") or "").startswith("m3u8")
               and f.get("vcodec") != "none" and f.get("acodec") != "none"
               and isinstance(f.get("height"), int) and f["height"] <= max_height and f.get("url")]
    if not options:
        raise UserError("No se encontró el video del directo", "Prueba otra calidad en Ajustes o vuelve a intentarlo")
    return max(options, key=lambda f: (f["height"], f.get("tbr") or 0))


def stream_url(ch: Channel, max_height: int, *, extract: Callable[[str], dict] | None = None) -> tuple[str, dict]:
    """(media playlist url, info) to capture. Raises Offline when the channel is not live."""
    try:
        info = (extract or _default_extract)(ch.url)
    except Exception as e:
        if _is_offline_error(e):
            raise Offline() from None
        log.warning("No se pudo abrir %s/%s (%s)", ch.platform, ch.handle, type(e).__name__)
        raise UserError("No se pudo abrir el directo", "Comprueba tu conexión y vuelve a intentarlo") from None
    if not info or info.get("is_live") is not True:
        raise Offline()
    return pick_format(info, max_height)["url"], info
