from __future__ import annotations

import base64
from pathlib import Path

from ..llm._http import decode_json, request
from ..models import Profile, UserError, Word
from .timing import chars_to_words

PROVIDER = "ElevenLabs"
BASE = "https://api.elevenlabs.io/v1"
MODEL = "eleven_multilingual_v2"


def list_voices(language: str, settings: dict) -> list[dict]:
    resp = request("get", f"{BASE}/voices", PROVIDER, headers={"xi-api-key": settings["elevenlabs_key"]})
    voices = decode_json(resp, PROVIDER).get("voices") or []
    return [
        {"id": v["voice_id"], "name": v.get("name", v["voice_id"]),
         "gender": (v.get("labels") or {}).get("gender", ""), "free": False}
        for v in voices
    ]


def synth_scene(text: str, profile: Profile, settings: dict, out: Path) -> list[Word] | None:
    """Write MP3 to `out`; return word timings that already carry punctuation.

    profile.rate is ignored: the with-timestamps endpoint has no speed control.
    """
    resp = request(
        "post", f"{BASE}/text-to-speech/{profile.voice}/with-timestamps", PROVIDER, profile.voice,
        headers={"xi-api-key": settings["elevenlabs_key"]},
        json={"text": text, "model_id": MODEL},
    )
    body = decode_json(resp, PROVIDER)
    try:
        out.write_bytes(base64.b64decode(body["audio_base64"]))
    except (KeyError, TypeError, ValueError):
        raise UserError("ElevenLabs devolvió una respuesta inesperada", "Inténtalo de nuevo") from None
    al = body.get("alignment") or {}
    return chars_to_words(text, al.get("character_start_times_seconds") or [],
                          al.get("character_end_times_seconds") or []) or None
