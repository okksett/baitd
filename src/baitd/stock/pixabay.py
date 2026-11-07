from __future__ import annotations

from ..llm._http import decode_json, request
from ..models import Media
from .quality import fit, min_dims, smallest_passing, to_int

PROVIDER = "Pixabay"
VIDEOS_URL = "https://pixabay.com/api/videos/"
PHOTOS_URL = "https://pixabay.com/api/"


def _get(url: str, query: str, settings: dict, extra: dict) -> dict:
    # Pixabay only accepts the key as a query parameter, so the request URL must never be logged
    # or put in an error; the shared request helper reports failures by provider name/status only.
    params = {"key": settings.get("pixabay_key", ""), "q": query, "per_page": 50, **extra}
    return decode_json(request("get", url, PROVIDER, params=params), PROVIDER)


def search_videos(query: str, orientation: str, settings: dict,
                  out_w: int = 1080, out_h: int = 1920) -> list[Media]:
    data = _get(VIDEOS_URL, query, settings, {"safesearch": "true"})  # videos have no orientation filter
    out = []
    for hit in data.get("hits") or []:
        cands = [(r.get("url") or "", to_int(r.get("width")), to_int(r.get("height")))
                 for r in (hit.get("videos") or {}).values() if isinstance(r, dict)]
        best = smallest_passing(cands, out_w, out_h)
        if best:
            url, w, h = best
            out.append(Media(f"pixabay:v:{hit['id']}", "pixabay", "video", url, w, h,
                             float(hit.get("duration") or 0)))
    return out


def search_photos(query: str, orientation: str, settings: dict,
                  out_w: int = 1080, out_h: int = 1920) -> list[Media]:
    min_w, min_h = min_dims(out_w, out_h)
    extra = {"image_type": "photo", "safesearch": "true", "min_width": min_w, "min_height": min_h}
    if orientation in ("portrait", "landscape"):
        extra["orientation"] = "vertical" if orientation == "portrait" else "horizontal"
    data = _get(PHOTOS_URL, query, settings, extra)
    out = []
    for hit in data.get("hits") or []:
        w, h = to_int(hit.get("imageWidth")), to_int(hit.get("imageHeight"))
        if w <= 0 or h <= 0:
            continue
        # largeImageURL is capped at 1280 px, fullHDURL at 1920 px (the latter may be absent).
        cands = [(hit.get("largeImageURL", ""), *fit(w, h, 1280, 1280)),
                 (hit.get("fullHDURL", ""), *fit(w, h, 1920, 1920))]
        best = smallest_passing(cands, out_w, out_h)
        if best:
            url, bw, bh = best
            out.append(Media(f"pixabay:p:{hit['id']}", "pixabay", "photo", url, bw, bh, None))
    return out
