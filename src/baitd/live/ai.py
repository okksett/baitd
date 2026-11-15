"""The AI's read of a live moment: its kind, a title, description and hashtags, and (for an automatic
candidate) the span worth keeping inside the window it was shown. One short request per moment; the user's
own marks are asked about only on demand, and the AI never moves a span the user chose."""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..clip import pipeline as clip_pipeline
from ..clip.moments import TITLE_WORDS, _parse_time, _sentences, _window_lines
from ..models import Word

__all__ = ["AI_KINDS", "AiVerdict", "build_prompt", "parse_reply", "window_lines", "HourlyLimit"]

AI_KINDS = {"humor": "gracioso", "polemica": "polemico", "nada": "nada"}
_HOUR_S = 3600.0


@dataclass
class AiVerdict:
    kind: str  # "gracioso", "polemico" or "nada"
    start: float | None  # absolute seconds; None when the span was not asked for
    end: float | None
    title: str
    description: str
    hashtags: list[str] = field(default_factory=list)


def window_lines(words: list[Word], w0: float, w1: float) -> str:
    """The sentences heard in [w0, w1] as `[mm:ss] text` lines, the marks counted from w0."""
    rel = [Word(w.text, max(0.0, w.start - w0), max(0.0, w.end - w0)) for w in words if w.end > w0 and w.start < w1]
    return _window_lines(_sentences(rel), rel, 0.0, w1 - w0)


def build_prompt(lines: str, language: str, *, want_span: bool) -> str:
    """Ask for one JSON object about a moment of a live stream; inicio/fin only when `want_span`."""
    span_rule = ([
        "inicio is the mark of the FIRST line worth keeping and fin the mark of the LAST one (that line's own "
        "mark, not the time after it). Copy the marks exactly.",
    ] if want_span else [])
    schema = ('{"tipo":"humor|polemica|nada",' + ('"inicio":"m:ss","fin":"m:ss",' if want_span else "")
              + '"titulo":"...","descripcion":"...","hashtags":["..."]}')
    return "\n".join([
        "You review a moment of a live stream to cut it into a vertical short clip (TikTok, Reels, Shorts).",
        "Below is what was said, one sentence per line, as [m:ss] text counted from the start of the moment.",
        "tipo is humor when it is funny, polemica when it is controversial or a heated argument, "
        "nada when it is neither.",
        *span_rule,
        f"Write titulo (at most {TITLE_WORDS} words), descripcion (one sentence) and 3 to 6 hashtags "
        f"in the language of the transcript: {language}.",
        f"Reply with JSON only, no commentary: {schema}",
        "",
        "TRANSCRIPT:",
        lines,
    ])


def _object(raw: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        data = json.loads(text[a:b + 1])
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _span(data: dict, w0: float, w1: float) -> tuple[float, float]:
    """The asked span made absolute and clamped into the window; the whole window when it is unreadable or
    shorter than a clip can be."""
    try:
        start = min(max(w0 + _parse_time(data.get("inicio")), w0), w1)
        end = min(max(w0 + _parse_time(data.get("fin")), w0), w1)
    except ValueError:
        return w0, w1
    if end - start < clip_pipeline.MIN_CLIP_S:
        return w0, w1
    return start, end


def parse_reply(raw: str, w0: float, w1: float, *, want_span: bool) -> AiVerdict | None:
    """The reply as an AiVerdict; None when it is not a readable answer."""
    data = _object(raw) if isinstance(raw, str) else None
    if data is None:
        return None
    tipo = data.get("tipo")
    kind = AI_KINDS.get(tipo.strip().lower()) if isinstance(tipo, str) else None
    title, desc, tags = data.get("titulo", ""), data.get("descripcion", ""), data.get("hashtags", [])
    if kind is None or not isinstance(title, str) or not isinstance(desc, str) or not isinstance(tags, list):
        return None
    start, end = _span(data, w0, w1) if want_span else (None, None)
    tags = [t.strip().lstrip("#") for t in tags if isinstance(t, str)]
    return AiVerdict(kind=kind, start=start, end=end, title=" ".join(title.split()[:TITLE_WORDS]),
                     description=desc.strip(), hashtags=["#" + re.sub(r"\s+", "", t) for t in tags if t])


class HourlyLimit:
    """At most `per_hour` AI requests in any sliding hour; exhaust() stops them until the session ends."""

    def __init__(self, per_hour: int, clock: Callable[[], float] = time.time):
        self._per_hour, self._clock = per_hour, clock
        self._taken: deque[float] = deque()
        self._exhausted = False
        self._lock = threading.Lock()

    def _prune(self) -> None:
        now = self._clock()
        while self._taken and now - self._taken[0] >= _HOUR_S:
            self._taken.popleft()

    def take(self) -> bool:
        """Use one request; False when none is left."""
        with self._lock:
            self._prune()
            if self._exhausted or len(self._taken) >= self._per_hour:
                return False
            self._taken.append(self._clock())
            return True

    def left(self) -> int:
        with self._lock:
            self._prune()
            return 0 if self._exhausted else max(0, self._per_hour - len(self._taken))

    def exhaust(self) -> None:
        """The provider's quota is gone (QuotaExhausted): no more requests this session."""
        with self._lock:
            self._exhausted = True
