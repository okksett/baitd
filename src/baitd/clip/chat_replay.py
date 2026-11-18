"""The chat of a finished stream, and where it peaked. A stream's best moments are where its chat suddenly
writes much faster or laughs; finding them from the chat costs nothing and needs no AI over hours of talk.

Two files are read: the chat yt-dlp saves for a YouTube stream with chat replay (`*.live_chat.json`, one
JSON object per line) and the JSON the open-source TwitchDownloader app exports for a Twitch VOD. Peaks use
the live detector's own chat rules (live.signals.ChatSignal), so a finished stream is judged like a live one.
"""
from __future__ import annotations

import json
import secrets
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..live.signals import BIN_S, ChatSignal
from ..models import UserError

__all__ = ["Peak", "parse", "save", "load", "peaks", "MAX_MESSAGES", "MIN_MESSAGES"]

MAX_MESSAGES = 2_000_000  # more is not a chat file
MIN_MESSAGES = 50  # fewer cannot show a peak
MIN_GAP_S = 90.0  # two peaks closer than this are one moment


@dataclass
class Peak:
    t: float  # start of the 10 s bin where the chat peaked, seconds into the video
    reason: str  # "laughs" or "chat"
    messages: int  # messages in that bin (the ranking)


def _bad_file() -> UserError:
    return UserError("Ese archivo no es un chat que Baitd sepa leer",
                     "Usa el JSON de TwitchDownloader o el chat que guarda yt-dlp")


def _youtube_text(renderer: dict) -> str:
    parts = []
    for run in (renderer.get("message") or {}).get("runs") or []:
        if "text" in run:
            parts.append(str(run["text"]))
        elif isinstance(run.get("emoji"), dict):
            e = run["emoji"]
            shortcuts = e.get("shortcuts") or []
            parts.append(str(e.get("emojiId") if len(str(e.get("emojiId", ""))) <= 2 else (shortcuts or [""])[0]))
    return "".join(parts)


def _youtube_lines(text: str) -> list[tuple[float, str]]:
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        action = json.loads(line).get("replayChatItemAction") or {}
        t = int(action.get("videoOffsetTimeMsec") or 0) / 1000
        for a in action.get("actions") or []:
            item = (a.get("addChatItemAction") or {}).get("item") or {}
            r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if r:
                out.append((t, _youtube_text(r)))
    return out


def _twitch(data: dict) -> list[tuple[float, str]]:
    return [(float(c["content_offset_seconds"]), str((c.get("message") or {}).get("body") or ""))
            for c in data["comments"]]


def parse(text: str) -> list[tuple[float, str]]:
    """(seconds into the video, message) in time order, from either file. UserError when it is neither."""
    try:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None  # YouTube's file is one object per line
        if isinstance(data, dict) and isinstance(data.get("comments"), list):
            messages = _twitch(data)
        elif data is None or (isinstance(data, dict) and "replayChatItemAction" in data):
            messages = _youtube_lines(text)
        else:
            raise _bad_file()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _bad_file() from None
    messages = [(t, m) for t, m in messages if t >= 0]
    if len(messages) < MIN_MESSAGES:
        raise UserError("Ese chat tiene muy pocos mensajes para encontrar momentos",
                        "Usa el chat completo del directo")
    if len(messages) > MAX_MESSAGES:
        raise _bad_file()
    return sorted(messages, key=lambda x: x[0])


def _dir() -> Path:
    d = config.data_dir() / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save(messages: list[tuple[float, str]]) -> str:
    chat_id = secrets.token_hex(6)
    config._write_json(_dir() / f"{chat_id}.json", [[round(t, 3), m] for t, m in messages])
    return chat_id


def load(chat_id: str) -> list[tuple[float, str]]:
    if not isinstance(chat_id, str) or len(chat_id) != 12 or not all(c in "0123456789abcdef" for c in chat_id):
        raise _bad_file()
    try:
        return [(float(t), str(m)) for t, m in json.loads((_dir() / f"{chat_id}.json").read_text("utf-8"))]
    except (OSError, ValueError, TypeError):
        raise UserError("El chat subido ya no está", "Vuelve a subir el archivo del chat") from None


def peaks(messages: list[tuple[float, str]], want: int) -> list[Peak]:
    """The `want` strongest moments: every spike the live rules see, ranked by messages in its bin (a laughing
    spike first when two tie), at most one per MIN_GAP_S."""
    if not messages:
        return []
    signal, found = ChatSignal(), []
    counts = Counter(int(t // BIN_S) for t, _ in messages)
    nxt = (int(messages[0][0] // BIN_S) + 1) * BIN_S
    for t, m in messages + [(messages[-1][0] + BIN_S, "")]:  # the sentinel judges the last full bin
        while t >= nxt:
            spike = signal.check(nxt)
            if spike is not None:
                found.append(Peak(spike.t, spike.reason, counts[int(spike.t // BIN_S)]))
            nxt += BIN_S
        if m:
            signal.add(t, m)
    found.sort(key=lambda p: (-p.messages, p.reason != "laughs", p.t))
    chosen: list[Peak] = []
    for p in found:
        if all(abs(p.t - q.t) >= MIN_GAP_S for q in chosen):
            chosen.append(p)
        if len(chosen) == want:
            break
    return chosen
