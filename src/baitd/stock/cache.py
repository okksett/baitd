from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..config import data_dir
from ..llm._http import request
from ..models import Media, UserError

CHUNK = 1 << 16
_EXTS = {".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".webp"}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def cache_path(m: Media) -> Path:
    ext = Path(urlparse(m.url).path).suffix.lower()
    if ext not in _EXTS:
        ext = ".mp4" if m.kind == "video" else ".jpg"
    return data_dir() / "cache" / f"{_safe(m.source)}_{m.kind}_{_safe(m.id.split(':')[-1])}{ext}"


def download(m: Media) -> Path:
    """Download m into the clip cache (once) and return its path; also sets m.path."""
    final = cache_path(m)
    if final.exists():
        m.path = final
        return final
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    name = m.source.capitalize()
    try:
        resp = request("get", m.url, name, stream=True)
    except UserError as e:  # the shared mapping talks about keys/models, which do not apply to a CDN file
        raise UserError(f"No se pudo descargar un clip de {name}", e.fix) from None
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
