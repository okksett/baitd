from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

from .clip.models import ClipProfile
from .ffmpeg import JobCancelled

__all__ = [
    "Scene", "Script", "Word", "Voiceover", "Media", "Shot", "SubtitleStyle",
    "Profile", "RenderOptions", "JobCancelled", "UserError", "ProgressFn", "first_sentence",
]

log = logging.getLogger(__name__)

_SENTENCE_END = ".?!…"
_CLOSERS = _SENTENCE_END + "\"'»”’)]"


def first_sentence(text: str) -> str:
    """The first sentence of `text`: up to its first . ? ! or … (plus closing marks), or all of it.

    The hook is always the first sentence of scene 1 (ruling R28), so this is the one definition
    shared by the LLM parser, the server and the captions."""
    t = text.strip()
    for i, ch in enumerate(t):
        if ch in _SENTENCE_END:
            j = i + 1
            while j < len(t) and t[j] in _CLOSERS:
                j += 1
            return t[:j]
    return t


@dataclass
class Scene:
    text: str
    keywords: list[str]  # English, 2-3

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Scene:
        return cls(text=d["text"], keywords=list(d["keywords"]))


@dataclass
class Script:
    title: str
    hook: str
    scenes: list[Scene]
    description: str
    hashtags: list[str]
    language: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Script:
        return cls(
            title=d["title"], hook=d["hook"],
            scenes=[Scene.from_dict(s) for s in d["scenes"]],
            description=d["description"], hashtags=list(d["hashtags"]),
            language=d["language"],
        )


@dataclass
class Word:
    text: str
    start: float  # seconds
    end: float


@dataclass
class Voiceover:
    path: Path
    duration: float
    words: list[Word]
    scene_bounds: list[tuple[float, float]]


@dataclass
class Media:
    id: str
    source: str
    kind: Literal["video", "photo"]
    url: str
    width: int
    height: int
    duration: float | None
    path: Path | None = None


@dataclass
class Shot:
    media: Media
    start: float
    end: float
    zoom_from: float = 1.0
    zoom_to: float = 1.08


@dataclass
class SubtitleStyle:
    preset: str
    font: str
    size: int
    color: str
    highlight: str
    outline: int
    box: bool
    position: Literal["bottom", "middle", "top"]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SubtitleStyle:
        if not isinstance(d, dict):
            raise TypeError("subtitle must be an object")
        from .config import default_profile

        base = default_profile().subtitle.to_dict()
        base.update({k: v for k, v in d.items() if k in base})
        return cls(**base)


@dataclass
class Profile:
    id: str
    name: str
    language: str
    aspect: Literal["9:16", "16:9"]
    fps: Literal[30, 60]
    seconds: Literal[30, 45, 60]
    llm_provider: str
    llm_model: str
    tone: str
    tts_provider: str
    voice: str
    rate: int
    subtitle: SubtitleStyle
    music: str  # "none" | "<mood>" | "<file path>"
    music_volume: float  # 0..1
    clipping: ClipProfile = field(default_factory=ClipProfile)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Profile:
        """Unknown keys are ignored; missing keys come from default_profile().

        Raises TypeError/ValueError for unusable entries (no id/name, wrong types).
        """
        if not isinstance(d, dict):
            raise TypeError("profile must be an object")
        from .config import default_profile

        base = default_profile().to_dict()
        base.update({k: v for k, v in d.items() if k in base})
        base["subtitle"] = SubtitleStyle.from_dict(d.get("subtitle", base["subtitle"]))
        try:
            base["clipping"] = ClipProfile.from_dict(d.get("clipping", {}))
        except ValueError as e:  # a bad clipping block must not cost the user the whole profile
            log.warning("Ajustes de clipping no válidos en el perfil %r (%s): se usan los de por defecto",
                        d.get("name"), e)
            base["clipping"] = ClipProfile()
        for key in ("id", "name"):
            if not isinstance(d.get(key), str) or not d[key]:
                raise ValueError(f"profile needs a non-empty {key}")
        if base["aspect"] not in ("9:16", "16:9"):
            raise ValueError("bad aspect")
        if base["fps"] not in (30, 60) or base["seconds"] not in (30, 45, 60):
            raise ValueError("bad fps/seconds")
        if not isinstance(base["rate"], int) or not isinstance(base["music_volume"], (int, float)):
            raise TypeError("bad rate/music_volume")
        return cls(**base)


@dataclass
class RenderOptions:
    width: int
    height: int
    fps: int
    encoder: str


class UserError(Exception):
    """User-facing error; str() is the 'cause → fix' text shown in the UI."""

    def __init__(self, cause: str, fix: str):
        self.cause = cause
        self.fix = fix
        super().__init__(f"{cause} → {fix}")


# (step_key, step_index 1..6, total=6, percent 0..100 within the step)
ProgressFn = Callable[[str, int, int, float], None]
