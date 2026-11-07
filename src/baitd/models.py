from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal

from .ffmpeg import JobCancelled

__all__ = [
    "Scene", "Script", "Word", "Voiceover", "Media", "Shot", "SubtitleStyle",
    "Profile", "RenderOptions", "JobCancelled", "UserError", "ProgressFn",
]


@dataclass
class Scene:
    text: str
    keywords: list[str]  # English, 2-3


@dataclass
class Script:
    title: str
    hook: str
    scenes: list[Scene]
    description: str
    hashtags: list[str]
    language: str


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
        return cls(**d)


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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Profile:
        d = dict(d)
        d["subtitle"] = SubtitleStyle.from_dict(d["subtitle"])
        return cls(**d)


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
