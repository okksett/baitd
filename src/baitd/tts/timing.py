"""Word-timing helpers: provider events -> Word lists, aligned to the script's own tokens."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from ..models import Word

MIN_MATCH_RATIO = 0.6  # below this share of matched tokens, alignment is replaced by an estimate


def edge_boundaries_to_words(events: list[dict]) -> list[Word]:
    """edge-tts WordBoundary events (offset/duration in 100 ns units) -> Words in seconds."""
    return [
        Word(e["text"], e["offset"] / 1e7, (e["offset"] + e["duration"]) / 1e7)
        for e in events
    ]


def chars_to_words(text: str, starts: list[float], ends: list[float]) -> list[Word]:
    """ElevenLabs per-character alignment -> one Word per whitespace-separated token."""
    if not starts or not ends:
        return []
    last = min(len(starts), len(ends)) - 1
    return [
        Word(m.group(), starts[min(m.start(), last)], ends[min(m.end() - 1, last)])
        for m in re.finditer(r"\S+", text)
    ]


def estimate_words(text: str, start: float, end: float) -> list[Word]:
    """Spread `text` tokens over [start, end] proportionally to their character length."""
    tokens = text.split()
    if not tokens:
        return []
    total = sum(len(t) for t in tokens)
    out: list[Word] = []
    acc = 0
    for i, t in enumerate(tokens):
        s = start + (end - start) * acc / total
        acc += len(t)
        e = end if i == len(tokens) - 1 else start + (end - start) * acc / total
        out.append(Word(t, s, e))
    return out


def scene_bounds(scene_word_counts: list[int], words: list[Word], total: float) -> list[tuple[float, float]]:
    """Contiguous scene spans covering [0, total]; a scene starts at its first word's start."""
    starts = [0.0]
    idx = 0
    for count in scene_word_counts[:-1]:
        idx += count
        starts.append(words[idx].start if idx < len(words) else total)
    ends = starts[1:] + [total]
    return list(zip(starts, ends))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if c.isalnum() and not unicodedata.combining(c))


def align_to_script(
    tokens: list[str], timed: list[Word], span: tuple[float, float] | None = None
) -> list[Word]:
    """Give every script token (punctuation intact) timing taken from `timed`.

    Tokens are matched in order on normalized alphanumerics. Unmatched tokens (numbers spoken as
    words, lone dashes...) share the gap between their matched neighbours. Never drops or reorders
    tokens. With poor alignment (< 60 % matched) the whole span is estimated instead.
    """
    if not tokens:
        return []
    if span is None:
        span = (timed[0].start, timed[-1].end) if timed else (0.0, 0.0)
    fallback = estimate_words(" ".join(tokens), *span)

    # Placeholders (a leading "#" never survives _norm) keep empty-normalized items (lone dashes...) from ever matching.
    a_seq = [_norm(t) or f"#t{i}" for i, t in enumerate(tokens)]
    b_seq = [_norm(w.text) or f"#w{i}" for i, w in enumerate(timed)]
    match_of: dict[int, int] = {}
    for blk in SequenceMatcher(None, a_seq, b_seq, autojunk=False).get_matching_blocks():
        for d in range(blk.size):
            match_of[blk.a + d] = blk.b + d

    if len(match_of) < MIN_MATCH_RATIO * len(tokens):
        return fallback

    out: list[Word] = []
    i = 0
    t_pos = 0  # first timed word not yet consumed
    prev_end = span[0]
    while i < len(tokens):
        if i in match_of:
            w = timed[match_of[i]]
            out.append(Word(tokens[i], w.start, w.end))
            t_pos = match_of[i] + 1
            prev_end = w.end
            i += 1
            continue
        k = i
        while k < len(tokens) and k not in match_of:
            k += 1
        q = match_of[k] if k < len(tokens) else len(timed)
        if q > t_pos:  # spread the run over the timed words nobody claimed
            lo, hi = timed[t_pos].start, timed[q - 1].end
        else:  # nothing left to claim: zero-width at the neighbour boundary
            lo = hi = prev_end if i > 0 else (timed[q].start if q < len(timed) else span[1])
        out.extend(estimate_words(" ".join(tokens[i:k]), lo, max(lo, hi)))
        i = k

    # Non-decreasing starts and ends; each end stays within [its start, next start].
    t = 0.0
    starts = []
    for w in out:
        t = max(w.start, t)
        starts.append(t)
    return [
        Word(w.text, starts[n], max(starts[n], min(w.end, starts[n + 1] if n + 1 < len(out) else w.end)))
        for n, w in enumerate(out)
    ]
