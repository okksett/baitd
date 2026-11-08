"""Shot timeline: cut planning on word boundaries and media assignment."""
from __future__ import annotations

from .models import Media, Shot, Word

_EPS = 1e-6
_SENTENCE_END = (".", "?", "!", "…")
_PUNCT_END = (".", "?", "!", "…", ",", ";", ":")
IDEAL_LEN = 3.0
PUNCT_WINDOW = 0.5


def _merge_short_scenes(
    bounds: list[tuple[float, float]], min_len: float
) -> list[tuple[int, float, float]]:
    """Scenes shorter than min_len join the next scene (keeping the earlier index)."""
    segments: list[tuple[int, float, float]] = []
    carry: tuple[int, float] | None = None
    for i, (start, end) in enumerate(bounds):
        if carry is not None:
            idx, start = carry
            carry = None
        else:
            idx = i
        if end - start < min_len - _EPS and i < len(bounds) - 1:
            carry = (idx, start)
            continue
        segments.append((idx, start, end))
    return segments


def _snap(
    ideal: float, lo: float, hi: float, ends: list[tuple[float, bool]]
) -> float:
    """Pick a cut in [lo, hi]: punctuation word end within +-0.5 s of ideal,
    else the nearest word end, else the ideal time itself."""
    ideal = min(max(ideal, lo), hi)
    cands = [(e, p) for e, p in ends if lo - _EPS <= e <= hi + _EPS]
    punct = [e for e, p in cands if p and abs(e - ideal) <= PUNCT_WINDOW]
    if punct:
        return min(punct, key=lambda e: abs(e - ideal))
    if cands:
        return min((e for e, _ in cands), key=lambda e: abs(e - ideal))
    return ideal


def _hook_end(words: list[Word], scene_end: float, hook_max: float) -> float | None:
    """End of the first sentence of scene 0, clamped to [1.0, hook_max]."""
    end = scene_end
    for w in words:
        if w.end > scene_end + _EPS:
            break
        if w.text.rstrip("\"')»”").endswith(_SENTENCE_END):
            end = w.end
            break
    return min(max(end, 1.0), hook_max)


def plan_cuts(
    scene_bounds: list[tuple[float, float]],
    words: list[Word],
    min_len: float = 2.0,
    max_len: float = 4.0,
    hook_max: float = 2.5,
) -> list[tuple[int, float, float]]:
    """Return contiguous (scene_index, start, end) shots covering all scenes."""
    if not scene_bounds:
        return []
    ends = [(w.end, w.text.rstrip("\"')»”").endswith(_PUNCT_END)) for w in words]
    segments = _merge_short_scenes(scene_bounds, min_len)
    shots: list[list] = []  # [scene_idx, start, end, protected]

    si = -1
    while si + 1 < len(segments):
        si += 1
        idx, seg_start, seg_end = segments[si]
        pos = seg_start
        last_segment = si == len(segments) - 1

        if si == 0:
            target = _hook_end(words, scene_bounds[0][1], hook_max)
            lo = seg_start + 1.0
            cut = _snap(target, lo, hook_max, ends) if target < seg_end else seg_end
            cut = min(cut, seg_end)
            shots.append([idx, pos, cut, True])
            pos = cut
            if 0 < seg_end - pos < min_len - _EPS and not last_segment:
                # R15: short remainder after the hook joins the next scene's first shot
                _, _, nxt_end = segments[si + 1]
                segments[si + 1] = (idx, pos, nxt_end)
                continue

        while seg_end - pos > max_len + _EPS:
            lo = pos + min_len
            hi = min(pos + max_len, seg_end - min_len)
            cut = _snap(pos + IDEAL_LEN, lo, hi, ends)
            shots.append([idx, pos, cut, False])
            pos = cut

        if seg_end - pos <= _EPS:
            continue
        if seg_end - pos >= min_len - _EPS or not shots:
            shots.append([idx, pos, seg_end, False])
            continue

        # short tail: merge into the previous shot, or re-split the last two evenly
        prev = shots[-1]
        if prev[3] or not (last_segment or prev[0] == idx):
            shots.append([idx, pos, seg_end, False])  # cannot touch hook / earlier scene
        elif seg_end - prev[1] <= max_len + _EPS:
            prev[2] = seg_end
        else:
            mid = _snap((prev[1] + seg_end) / 2, prev[1] + min_len, seg_end - min_len, ends)
            prev[2] = mid
            shots.append([idx, mid, seg_end, False])

    # shots after a merged short scene belong to the scene they start in
    starts = [b[0] for b in scene_bounds]
    out: list[tuple[int, float, float]] = []
    seg_firsts = {s[1] for s in segments}
    for idx, start, end, _ in shots:
        if start not in seg_firsts:
            idx = max(i for i, s in enumerate(starts) if s <= start + _EPS)
        out.append((idx, start, end))
    return out


def assign_media(
    cuts: list[tuple[int, float, float]], media_by_scene: dict[int, list[Media]]
) -> list[Shot]:
    """Give each cut an unused Media: its scene's list first, else the nearest scene's."""
    queues = {s: list(items) for s, items in media_by_scene.items()}
    shots: list[Shot] = []
    for n, (scene, start, end) in enumerate(cuts):
        options = [s for s, q in queues.items() if q]
        if not options:
            raise ValueError("not enough media")
        pick = scene if queues.get(scene) else min(options, key=lambda s: (abs(s - scene), s))
        media = queues[pick].pop(0)
        zoom = (1.0, 1.08) if n % 2 == 0 else (1.08, 1.0)
        shots.append(Shot(media, start, end, zoom[0], zoom[1]))
    return shots
