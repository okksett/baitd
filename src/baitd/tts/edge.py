from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from ..models import Profile, UserError, Word
from .timing import edge_boundaries_to_words


def _failure(e: Exception) -> UserError:
    return UserError(f"Edge TTS no pudo generar la voz ({type(e).__name__})",
                     "Comprueba tu conexión a internet o prueba otra voz")


async def _stream(text: str, voice: str, rate: int, out: Path) -> list[dict]:
    comm = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%", boundary="WordBoundary")
    events: list[dict] = []
    with open(out, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                events.append(chunk)
    return events


def synth_scene(text: str, profile: Profile, settings: dict, out: Path) -> list[Word]:
    """Write MP3 audio to `out`; return unpunctuated word timings (seconds, scene-local)."""
    try:
        events = asyncio.run(_stream(text, profile.voice, profile.rate, out))
    except Exception as e:
        raise _failure(e) from None
    if not out.exists() or out.stat().st_size == 0:
        raise UserError("Edge TTS no devolvió audio", "Prueba otra voz o inténtalo de nuevo")
    return edge_boundaries_to_words(events)


def list_voices(language: str, settings: dict) -> list[dict]:
    try:
        raw = asyncio.run(edge_tts.list_voices())
    except Exception as e:
        raise _failure(e) from None
    lang = language.lower()
    return [
        {"id": v["ShortName"], "name": v.get("FriendlyName") or v["ShortName"],
         "gender": v.get("Gender", "").lower(), "free": True}
        for v in raw if v.get("Locale", "").lower().startswith(lang)
    ]
