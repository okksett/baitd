from __future__ import annotations

from ..llm._http import decode_json, request
from ..models import Media
from .quality import fit, smallest_passing, to_int

PROVIDER = "Pexels"
VIDEOS_URL = "https://api.pexels.com/videos/search"
PHOTOS_URL = "https://api.pexels.com/v1/search"


def _get(url: str, query: str, orientation: str, settings: dict, extra: dict) -> dict:
    params = {"query": query, "per_page": 40, **extra}
    if orientation in ("portrait", "landscape"):
        params["orientation"] = orientation
    headers = {"Authorization": settings.get("pexels_key", "")}
    return decode_json(request("get", url, PROVIDER, headers=headers, params=params), PROVIDER)


def search_videos(query: str, orientation: str, settings: dict,
                  out_w: int = 1080, out_h: int = 1920) -> list[Media]:
    data = _get(VIDEOS_URL, query, orientation, settings, {"size": "medium"})
    out = []
    for v in data.get("videos") or []:
        cands = [(f.get("link") or "", to_int(f.get("width")), to_int(f.get("height")))
                 for f in v.get("video_files") or [] if f.get("file_type") == "video/mp4"]
        best = smallest_passing(cands, out_w, out_h)
        if best:
            url, w, h = best
            out.append(Media(f"pexels:v:{v['id']}", "pexels", "video", url, w, h, float(v.get("duration") or 0)))
    return out


def search_photos(query: str, orientation: str, settings: dict,
                  out_w: int = 1080, out_h: int = 1920) -> list[Media]:
    data = _get(PHOTOS_URL, query, orientation, settings, {})
    out = []
    for p in data.get("photos") or []:
        w, h, src = to_int(p.get("width")), to_int(p.get("height")), p.get("src") or {}
        if w <= 0 or h <= 0:
            continue
        # Pexels only gives the original size; the resized variants are fitted into a fixed box.
        cands = [(src.get("large", ""), *fit(w, h, 940, 650)),
                 (src.get("large2x", ""), *fit(w, h, 1880, 1300)),
                 (src.get("original", ""), w, h)]
        best = smallest_passing(cands, out_w, out_h)
        if best:
            url, bw, bh = best
            out.append(Media(f"pexels:p:{p['id']}", "pexels", "photo", url, bw, bh, None))
    return out
