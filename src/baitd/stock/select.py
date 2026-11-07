from __future__ import annotations

from ..llm._http import AuthError
from ..models import Media, UserError
from . import pexels, pixabay
from .quality import passes_quality

__all__ = ["passes_quality", "score", "find_media"]


def score(m: Media, out_w: int, out_h: int, min_seconds: float) -> float:
    """Higher is better: same orientation as the output, enough length, videos over photos."""
    orient = 2.0 if (m.width > m.height) == (out_w > out_h) else 0.0
    if m.duration is None:
        length = 0.5
    else:
        length = min(m.duration / min_seconds, 1.0) if min_seconds > 0 else 1.0
    return orient + length


def find_media(keywords: list[str], need: int, min_seconds: float, out_w: int, out_h: int,
               used: set[str], settings: dict) -> list[Media]:
    """Up to `need` unique clips/photos not in `used` (the caller records them in `used`)."""
    providers = [p for p, key in ((pexels, "pexels_key"), (pixabay, "pixabay_key")) if settings.get(key)]
    if not providers:
        raise UserError("Falta la clave de Pexels o Pixabay", "Agrega una gratis en Ajustes")

    orient = "portrait" if out_h > out_w else "landscape"
    queries = [(k, orient) for k in keywords] + [(k, "any") for k in keywords]
    stages = [("search_videos", q) for q in queries]
    words = keywords[0].split() if keywords else []
    if words and words[0] != keywords[0]:
        stages.append(("search_videos", (words[0], "any")))
    stages += [("search_photos", q) for q in queries]

    taken = set(used)
    result: list[Media] = []
    errors: list[UserError] = []
    dead: set = set()  # providers whose key was rejected; not queried again in this call
    for fn, (query, o) in stages:
        # Pexels first; Pixabay is only asked while the stage is still short of `need`.
        for prov in providers:
            if len(result) >= need:
                break
            if prov in dead:
                continue
            try:
                found = getattr(prov, fn)(query, o, settings, out_w=out_w, out_h=out_h)
            except UserError as e:
                errors.append(e)
                if isinstance(e, AuthError):
                    dead.add(prov)
                continue
            found = [m for m in found if m.id not in taken and passes_quality(m, out_w, out_h)]
            found.sort(key=lambda m: score(m, out_w, out_h, min_seconds), reverse=True)
            for m in found:
                if len(result) < need and m.id not in taken:
                    taken.add(m.id)
                    result.append(m)
        if len(result) >= need:
            break
    if not result:
        if errors:  # e.g. an invalid key is more useful than "nothing found"
            raise errors[0]
        raise UserError(f"No se encontraron clips para '{', '.join(keywords)}'",
                        "Prueba con otro tema o revisa las claves de Pexels/Pixabay")
    return result
