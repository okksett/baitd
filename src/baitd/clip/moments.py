"""Find viral-clip moments in a transcript: LLM proposals per window, snapped to sentence
boundaries, nudged by audio energy, de-duplicated and ranked."""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
import threading
from pathlib import Path
from typing import Callable

from .. import ffmpeg, llm
from ..ffmpeg import JobCancelled
from ..models import _SENTENCE_END, Profile, Word
from .models import KINDS, ClipProfile, Moment, Transcript
from .transcribe import no_speech

__all__ = ["WINDOW_S", "OVERLAP_S", "windows", "build_prompt", "parse_moments", "snap_to_sentences",
           "energy_curve", "energy_bonus", "find_moments"]

log = logging.getLogger(__name__)

WINDOW_S = 480.0
OVERLAP_S = 60.0
PAUSE_S = 0.8  # a silence this long ends a sentence even without punctuation (ruling T5a)
SILENCE_LUFS = -70.0
MAX_BONUS = 15
TITLE_WORDS = 8
DEDUPE_OVERLAP = 0.30

_TRAILING_CLOSERS = "\"'»”’)]"
_M_VALUE = re.compile(r"\bt:\s*(\d+(?:\.\d+)?)\s.*?\bM:\s*(-?\d+(?:\.\d+)?)")


# ---- sentences ----

def _sentences(words: list[Word]) -> list[tuple[float, float, int, int]]:
    """(start, end, first word index, last word index) per sentence. A sentence ends after a word
    whose text ends in . ? ! … (trailing quotes/brackets allowed) or before a pause >= PAUSE_S."""
    out: list[tuple[float, float, int, int]] = []
    first = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        t = w.text.rstrip(_TRAILING_CLOSERS)
        ends = bool(t) and t[-1] in _SENTENCE_END
        if last or ends or words[i + 1].start - w.end >= PAUSE_S:
            out.append((words[first].start, w.end, first, i))
            first = i + 1
    return out


def _ts(sec: float) -> str:
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_time(v) -> float:
    if isinstance(v, bool):
        raise ValueError("not a time")
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            raise ValueError("not a time")
        return float(v)
    if not isinstance(v, str):
        raise ValueError("not a time")
    parts = v.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("not a time")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if not math.isfinite(total) or total < 0:
        raise ValueError("not a time")
    return total


# ---- windows and prompt ----

def windows(t: Transcript) -> list[tuple[float, float]]:
    """Overlapping (start, end) windows of WINDOW_S seconds, stepping WINDOW_S - OVERLAP_S, covering the whole transcript."""
    dur = max(t.duration, t.words[-1].end if t.words else 0.0)
    out: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(start + WINDOW_S, dur)
        out.append((start, end))
        if end >= dur:
            return out
        start += WINDOW_S - OVERLAP_S


def build_prompt(lines: str, opts: ClipProfile, language: str) -> str:
    kinds = KINDS if opts.kind == "todos" else (opts.kind,)
    return "\n".join([
        "You find the best moments of a long video to cut into viral vertical short clips "
        "(TikTok, Reels, Shorts).",
        "Below is the transcript, one sentence per line, as [mm:ss] text (h:mm:ss after the first hour).",
        f"Pick the moments that would work best as standalone clips of {opts.min_s} to {opts.max_s} seconds.",
        "Score each from 0 to 100 on these criteria:",
        "- the first sentence hooks the viewer;",
        "- the clip is understandable without any context from the rest of the video;",
        "- it ends on the payoff (the punchline or the conclusion);",
        f"- its length fits {opts.min_s}-{opts.max_s} seconds.",
        "start is the mark of the FIRST line included in the clip; end is the mark of the LAST line "
        "included in the clip (that line's own mark, not the time after it). Copy the marks exactly.",
        f"kind is one of: {', '.join(kinds)}.",
        f"Write title (at most {TITLE_WORDS} words), description and hashtags in this language: {language}.",
        'Reply with JSON only, no commentary: {"moments":[{"start":"mm:ss","end":"mm:ss","kind":"...",'
        '"score":0-100,"title":"...","description":"...","hashtags":["..."]}]}',
        "Return an empty list if nothing is worth clipping.",
        "",
        "TRANSCRIPT:",
        lines,
    ])


def _window_lines(sentences, words: list[Word], w0: float, w1: float) -> str:
    rows = []
    for s, _e, i0, i1 in sentences:
        if w0 <= s < w1:
            rows.append(f"[{_ts(s)}] {' '.join(w.text for w in words[i0:i1 + 1])}")
    return "\n".join(rows)


# ---- snapping ----

def snap_to_sentences(start: float, end: float, words: list[Word], min_s: float, max_s: float
                      ) -> tuple[float, float] | None:
    """Move start to the start of its sentence and end to the end of its sentence, then shrink
    (drop whole sentences from the end) or grow (add whole sentences) until the length is in
    [min_s, max_s]. None when impossible."""
    sents = _sentences(words)
    if not sents or end <= start:
        return None
    si = 0
    for k, s in enumerate(sents):
        if s[0] <= start + 1e-6:
            si = k
    ei = si
    for k, s in enumerate(sents):
        if s[0] < end - 1e-6:
            ei = k
    ei = max(ei, si)

    def length(a: int, b: int) -> float:
        return sents[b][1] - sents[a][0]

    while ei > si and length(si, ei) > max_s:
        ei -= 1
    while ei < len(sents) - 1 and length(si, ei) < min_s:
        ei += 1
    if not min_s <= length(si, ei) <= max_s:
        return None
    return sents[si][0], sents[ei][1]


# ---- parsing ----

def _from_marks(start: float, end: float, words: list[Word]) -> tuple[float, float]:
    """The prompt's marks are whole seconds (floored), so a whole-second start/end names the sentence
    starting in [mark, mark+1): start moves to that sentence's start, end to that sentence's end.
    Non-integral times (mid-sentence) are left to snap_to_sentences."""
    sents = _sentences(words)
    if start == int(start):
        for s0, _e, _i, _j in sents:
            if start <= s0 < start + 1:
                start = s0
                break
    if end == int(end):
        for s0, e0, _i, _j in sents:
            if end <= s0 < end + 1:
                end = e0
                break
    return start, end


def parse_moments(raw: str, t: Transcript, opts: ClipProfile) -> list[Moment]:
    text = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object found in the reply")
    try:
        data = json.loads(text[a:b + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from None
    items = data.get("moments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("'moments' must be a list")

    allowed = KINDS if opts.kind == "todos" else (opts.kind,)
    out: list[Moment] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            start, end = _parse_time(it.get("start")), _parse_time(it.get("end"))
        except ValueError:
            continue
        score = it.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            continue
        kind = it.get("kind")
        kind = kind.strip().lower() if isinstance(kind, str) else ""
        if kind not in allowed:
            continue
        start, end = _from_marks(start, end, t.words)
        snapped = snap_to_sentences(start, end, t.words, opts.min_s, opts.max_s)
        if snapped is None:
            continue
        s, e = snapped
        span = " ".join(w.text for w in t.words if s - 1e-6 <= w.start < e)
        title = it.get("title")
        title = " ".join(title.split()[:TITLE_WORDS]) if isinstance(title, str) else ""
        desc = it.get("description")
        tags = it.get("hashtags")
        tags = [x.strip().lstrip("#") for x in tags if isinstance(x, str)] if isinstance(tags, list) else []
        out.append(Moment(
            start=s, end=e, kind=kind, score=max(0, min(100, round(score))),
            title=title or " ".join(span.split()[:TITLE_WORDS]),
            description=desc.strip() if isinstance(desc, str) else "",
            hashtags=["#" + re.sub(r"\s+", "", x) for x in tags if x],
            text=span, framing=opts.framing,
        ))
    return out


# ---- audio energy ----

def energy_curve(audio: Path, cancel: threading.Event) -> list[float]:
    """Momentary loudness (LUFS), one value per second: the max of ebur128's M readings in that second.
    Seconds without a reading are -70."""
    best: dict[int, float] = {}

    def on_line(line: str) -> None:
        m = _M_VALUE.search(line)
        if m:
            sec = int(float(m.group(1)))
            best[sec] = max(best.get(sec, SILENCE_LUFS), SILENCE_LUFS, float(m.group(2)))

    ffmpeg.run(["-i", str(audio), "-vn", "-af", "ebur128", "-f", "null", "-"], cancel=cancel, on_line=on_line)
    if not best:
        return []
    return [best.get(i, SILENCE_LUFS) for i in range(max(best) + 1)]


def energy_bonus(m: Moment, curve: list[float]) -> int:
    """0..15: twice how many LU the moment's mean loudness sits above the video's median."""
    if not curve:
        return 0
    a = min(int(m.start), len(curve) - 1)
    b = max(a + 1, min(math.ceil(m.end), len(curve)))
    seg = curve[a:b]
    bonus = round(2 * (sum(seg) / len(seg) - statistics.median(curve)))
    return max(0, min(MAX_BONUS, bonus))


# ---- orchestration ----

def _overlap_frac(a: Moment, b: Moment) -> float:
    shorter = min(a.end - a.start, b.end - b.start)
    return max(0.0, min(a.end, b.end) - max(a.start, b.start)) / shorter if shorter > 0 else 1.0


def _dedupe(moments: list[Moment]) -> list[Moment]:
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda m: -m.score):
        if all(_overlap_frac(m, k) <= DEDUPE_OVERLAP for k in kept):
            kept.append(m)
    return kept


def _ask(prompt: str, t: Transcript, opts: ClipProfile, complete: Callable[[str], str]) -> list[Moment]:
    """One window: parse the reply, one repair retry on bad JSON (as generate_script does), else skip."""
    try:
        return parse_moments(complete(prompt), t, opts)
    except ValueError as first:
        retry = f"{prompt}\n\nReturn only valid JSON matching the schema. Your previous reply was rejected: {first}"
        try:
            return parse_moments(complete(retry), t, opts)
        except ValueError as second:
            log.warning("La IA no devolvió JSON válido para una ventana; se omite (%s)", second)
            return []


def find_moments(t: Transcript, audio: Path, opts: ClipProfile, profile: Profile, settings: dict,
                 want: int, cancel: threading.Event, on_progress: Callable[[float], None],
                 complete: Callable[[str], str] | None = None) -> list[Moment]:
    if complete is None:
        def complete(p: str) -> str:
            return llm.complete(p, profile, settings)

    try:
        curve = energy_curve(audio, cancel)
    except JobCancelled:
        raise
    except Exception as e:  # energy is only a bonus; never fail the job for it
        log.warning("No se pudo medir la energía del audio (%s)", e)
        curve = []

    sentences = _sentences(t.words)
    wins = windows(t)
    found: list[Moment] = []
    for i, (w0, w1) in enumerate(wins):
        if cancel.is_set():
            raise JobCancelled()
        lines = _window_lines(sentences, t.words, w0, w1)
        if lines:
            for m in _ask(build_prompt(lines, opts, t.language), t, opts, complete):
                m.score = min(100, m.score + energy_bonus(m, curve))
                found.append(m)
        on_progress((i + 1) / len(wins))
    if not found:
        raise no_speech()
    return sorted(_dedupe(found), key=lambda m: -m.score)[:want]
