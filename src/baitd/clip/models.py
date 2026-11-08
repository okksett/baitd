"""Clipping data model. Must not import baitd.models at module level: baitd.models imports
ClipProfile, so Word is only imported for typing and lazily inside Transcript.from_dict."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Word

__all__ = ["KINDS", "FRAMINGS", "TRANSCRIBERS", "WHISPER_MODELS", "ClipProfile", "SourceInfo", "Transcript", "Moment"]

KINDS = ("gracioso", "interesante", "polemico", "historia")  # moment types; "todos" = all
FRAMINGS = ("cara", "centro", "completo")
TRANSCRIBERS = ("openai", "local")
WHISPER_MODELS = ("small", "medium", "large-v3")

_LANG = re.compile(r"[a-z]{2,3}")


def _int_in(d: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = d.get(key, default)
    if type(v) is not int or not lo <= v <= hi:  # bool and float are not ints here
        raise ValueError(f"{key} must be an integer between {lo} and {hi}")
    return v


def _bool(d: dict, key: str, default: bool) -> bool:
    v = d.get(key, default)
    if type(v) is not bool:
        raise ValueError(f"{key} must be true or false")
    return v


def _choice(d: dict, key: str, default: str, allowed: tuple[str, ...]) -> str:
    v = d.get(key, default)
    if not isinstance(v, str) or v not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(allowed)}")
    return v


@dataclass
class ClipProfile:
    count: int = 5  # 1..20
    min_s: int = 20  # 10..180
    max_s: int = 60  # 10..180, >= min_s
    kind: str = "todos"  # "todos" | one of KINDS
    framing: str = "cara"
    hook_title: bool = True
    credit: bool = True
    music: bool = False
    transcriber: str = "local"
    whisper_model: str = "small"
    language: str = "auto"  # "auto" (detect) or an ISO code; passed to transcribe() as None when "auto"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ClipProfile:
        """Missing keys take defaults, unknown keys are ignored; bad values or types raise ValueError."""
        if not isinstance(d, dict):
            raise ValueError("clipping must be an object")
        base = cls()
        lang = d.get("language", base.language)
        if not isinstance(lang, str) or (lang != "auto" and not _LANG.fullmatch(lang)):
            raise ValueError("language must be 'auto' or a lowercase ISO code")
        c = cls(
            count=_int_in(d, "count", base.count, 1, 20),
            min_s=_int_in(d, "min_s", base.min_s, 10, 180),
            max_s=_int_in(d, "max_s", base.max_s, 10, 180),
            kind=_choice(d, "kind", base.kind, ("todos", *KINDS)),
            framing=_choice(d, "framing", base.framing, FRAMINGS),
            hook_title=_bool(d, "hook_title", base.hook_title),
            credit=_bool(d, "credit", base.credit),
            music=_bool(d, "music", base.music),
            transcriber=_choice(d, "transcriber", base.transcriber, TRANSCRIBERS),
            whisper_model=_choice(d, "whisper_model", base.whisper_model, WHISPER_MODELS),
            language=lang,
        )
        if c.min_s > c.max_s:
            raise ValueError("min_s must not exceed max_s")
        return c


@dataclass
class SourceInfo:
    id: str  # 12-hex, also the folder name under fuentes/
    path: Path  # media file
    title: str
    channel: str  # "" for uploads
    url: str  # "" for uploads
    duration: float
    width: int  # 0 for audio-only
    height: int
    fps: float
    has_video: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SourceInfo:
        return cls(
            id=d["id"], path=Path(d["path"]), title=d["title"], channel=d["channel"], url=d["url"],
            duration=float(d["duration"]), width=int(d["width"]), height=int(d["height"]),
            fps=float(d["fps"]), has_video=bool(d["has_video"]),
        )


@dataclass
class Transcript:
    language: str
    words: list[Word]  # absolute source times
    duration: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Transcript:
        from ..models import Word  # lazy: baitd.models imports this module

        return cls(
            language=d["language"],
            words=[Word(w["text"], float(w["start"]), float(w["end"])) for w in d["words"]],
            duration=float(d["duration"]),
        )


@dataclass
class Moment:
    start: float  # absolute source seconds, sentence-aligned
    end: float
    kind: str
    score: int  # 0..100
    title: str  # hook title, <= 8 words
    description: str
    hashtags: list[str]
    text: str  # transcript text of the span
    framing: str = "cara"
    credit: str = ""  # user-edited credit text; "" = auto "Fuente: @<channel>"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Moment:
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        kw["hashtags"] = list(kw.get("hashtags", []))
        return cls(**kw)
