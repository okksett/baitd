"""Clip cache: one file per (clip, rendition), least-recently-used files evicted over a size cap."""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..config import data_dir
from ..llm._http import request
from ..models import Media, UserError

log = logging.getLogger(__name__)

CHUNK = 1 << 16
_EXTS = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
DOWNLOAD_FIX = "Inténtalo de nuevo; si sigue fallando, prueba otro tema"
DEFAULT_MAX_GB = 3.0  # ruling R30; settings key cache_max_gb


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def cache_path(m: Media) -> Path:
    ext = Path(urlparse(m.url).path).suffix.lower()
    if ext not in _EXTS:
        ext = ".mp4" if m.kind == "video" else ".jpg"
    # the rendition size is part of the name: the same clip picked for 9:16 and for 16:9 is two
    # different files, so a smaller cached rendition is never reused for a bigger output
    return _dir() / (f"{_safe(m.source)}_{m.kind}_{_safe(m.id.split(':')[-1])}"
                     f"_{int(m.width)}x{int(m.height)}{ext}")


def _dir() -> Path:
    return data_dir() / "cache"


def max_bytes(settings: dict) -> int:
    """The cache cap from settings["cache_max_gb"] (GB, > 0); the default for missing/bad values."""
    value = settings.get("cache_max_gb")
    try:
        gb = float(value) if not isinstance(value, bool) else 0.0
    except (TypeError, ValueError):
        gb = 0.0
    if not gb > 0 or gb == float("inf"):
        gb = DEFAULT_MAX_GB
    return int(gb * 1024 ** 3)


def prune(limit: int) -> None:
    """Delete least-recently-used cache files (by mtime) until the cache is at most `limit` bytes."""
    try:
        entries = [(p.stat(), p) for p in _dir().iterdir() if p.is_file()]
    except FileNotFoundError:
        return
    entries.sort(key=lambda e: e[0].st_mtime)  # oldest first
    total = sum(st.st_size for st, _ in entries)
    for st, p in entries:
        if total <= limit:
            break
        try:
            p.unlink()
            total -= st.st_size
        except OSError:
            log.warning("No se pudo borrar %s de la caché", p.name)


def download(m: Media) -> Path:
    """Download m into the clip cache (once) and return its path; also sets m.path."""
    final = cache_path(m)
    if final.exists():
        try:
            os.utime(final)  # a hit counts as a use for the LRU cap
        except OSError:
            pass
        m.path = final
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    name = m.source.capitalize()
    try:
        resp = request("get", m.url, name, stream=True)
    except UserError:  # the shared mapping talks about keys/models, which do not apply to a CDN file
        raise UserError(f"No se pudo descargar un clip de {name}", DOWNLOAD_FIX) from None
    try:
        with open(part, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK):
                f.write(chunk)
        os.replace(part, final)
    except (requests.RequestException, OSError) as e:
        raise UserError(f"No se pudo descargar un clip de {name} ({type(e).__name__})",
                        "Comprueba tu conexión y el espacio en disco, e inténtalo de nuevo") from None
    finally:
        resp.close()
        try:
            part.unlink()
        except OSError:
            pass
    m.path = final
    return final
