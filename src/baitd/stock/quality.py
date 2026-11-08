from __future__ import annotations

from ..models import Media

ZOOM = 1.08
MAX_UPSCALE = 1.25


def cover_scale(w: int, h: int, out_w: int, out_h: int) -> float:
    """Scale needed for a w x h source to cover the output frame (inf if unusable)."""
    if w <= 0 or h <= 0:
        return float("inf")
    return max(out_w / w, out_h / h)


def passes_quality(m: Media, out_w: int, out_h: int, zoom: float = ZOOM,
                   max_upscale: float = MAX_UPSCALE) -> bool:
    return cover_scale(m.width, m.height, out_w, out_h) * zoom <= max_upscale


def smallest_passing(cands: list[tuple[str, int, int]], out_w: int,
                     out_h: int) -> tuple[str, int, int] | None:
    """Pick the smallest (url, w, h) candidate that passes the quality rule, or None."""
    ok = [c for c in cands if c[0] and cover_scale(c[1], c[2], out_w, out_h) * ZOOM <= MAX_UPSCALE]
    return min(ok, key=lambda c: c[1] * c[2]) if ok else None


def fit(w: int, h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Size of a w x h image scaled down (never up) to fit inside the box."""
    s = min(box_w / w, box_h / h, 1.0)
    return round(w * s), round(h * s)


def min_dims(out_w: int, out_h: int) -> tuple[int, int]:
    """Smallest source size that can pass the quality rule for this output."""
    k = ZOOM / MAX_UPSCALE
    return int(out_w * k), int(out_h * k)


def to_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
