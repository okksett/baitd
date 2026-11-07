from __future__ import annotations

from urllib.parse import quote

from ..models import UserError
from ._http import post_json, unexpected

BASE = "https://generativelanguage.googleapis.com/v1beta"


def complete(prompt: str, model: str, settings: dict) -> str:
    key = settings.get("gemini_key", "")
    if not key:
        raise UserError("Falta la clave de Gemini", "Añádela en Ajustes")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    # Key goes in a header, not the URL, so it never ends up in logs or tracebacks.
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    data = post_json(f"{BASE}/models/{quote(model, safe='')}:generateContent", headers, payload, "Gemini", model)
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError, AttributeError):
        raise unexpected("Gemini") from None
