from __future__ import annotations

from pathlib import Path

from ..llm._http import decode_json, request
from ..models import Profile, UserError, Word

PROVIDER = "OpenAI"
BASE = "https://api.openai.com/v1"
VOICES = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer"]


def speed_for(rate: int) -> float:
    return min(4.0, max(0.25, 1 + rate / 100))


def list_voices(language: str, settings: dict) -> list[dict]:
    return [{"id": v, "name": v.capitalize(), "gender": "", "free": False} for v in VOICES]


def synth_scene(text: str, profile: Profile, settings: dict, out: Path) -> list[Word] | None:
    """Write MP3 to `out`. Returns Whisper word timings (no punctuation), or None if transcription fails."""
    headers = {"Authorization": f"Bearer {settings['openai_key']}"}
    resp = request(
        "post", f"{BASE}/audio/speech", PROVIDER, "tts-1-hd", headers=headers,
        json={"model": "tts-1-hd", "input": text, "voice": profile.voice or "alloy",
              "speed": speed_for(profile.rate), "response_format": "mp3"},
    )
    out.write_bytes(resp.content)
    try:
        tr = request(
            "post", f"{BASE}/audio/transcriptions", PROVIDER, "whisper-1", headers=headers,
            files={"file": (out.name, resp.content, "audio/mpeg")},
            data={"model": "whisper-1", "response_format": "verbose_json",
                  "timestamp_granularities[]": "word"},
        )
        words = decode_json(tr, PROVIDER).get("words") or []
        return [Word(w["word"].strip(), float(w["start"]), float(w["end"])) for w in words] or None
    except (UserError, KeyError, TypeError, ValueError, AttributeError):
        return None
