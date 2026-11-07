"""Word-timing helpers: provider events -> Word lists, aligned to the script's own tokens."""
from __future__ import annotations

import re
import unicodedata

from ..models import Word

MIN_MATCH_RATIO = 0.6  # below this share of matched tokens, alignment is replaced by an estimate
_LOOKAHEAD = 4         # how many timed words a script token may skip over while matching


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


def _match_at(tok: str, timed_norm: list[str], j: int) -> int:
    """Number of timed words (1-3) starting at j whose joined text equals `tok`, else 0."""
    acc = ""
    for n in range(1, 4):
        if j + n > len(timed_norm):
            break
        acc += timed_norm[j + n - 1]
        if acc == tok:
            return n
        if not tok.startswith(acc):
            break
    return 0


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

    timed_norm = [_norm(w.text) for w in timed]
    matched: list[tuple[float, float] | None] = []
    j = 0
    for tok in tokens:
        key = _norm(tok)
        hit = None
        if key:
            for k in range(j, min(j + _LOOKAHEAD, len(timed))):
                n = _match_at(key, timed_norm, k)
                if n:
                    hit = (k, n)
                    break
        if hit:
            k, n = hit
            matched.append((timed[k].start, timed[k + n - 1].end))
            j = k + n
        else:
            matched.append(None)

    if sum(m is not None for m in matched) < MIN_MATCH_RATIO * len(tokens):
        return fallback

    out: list[Word] = []
    i = 0
    prev_end = span[0]
    while i < len(tokens):
        m = matched[i]
        if m is not None:
            out.append(Word(tokens[i], m[0], m[1]))
            prev_end = m[1]
            i += 1
            continue
        k = i
        while k < len(tokens) and matched[k] is None:
            k += 1
        nxt = matched[k][0] if k < len(tokens) else span[1]
        out.extend(estimate_words(" ".join(tokens[i:k]), prev_end, max(prev_end, nxt)))
        i = k

    # Guarantee non-decreasing times even if the provider's own timings overlap.
    t = 0.0
    fixed = []
    for w in out:
        s = max(w.start, t)
        e = max(w.end, s)
        fixed.append(Word(w.text, s, e))
        t = s
    return fixed
