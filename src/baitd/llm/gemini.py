from __future__ import annotations

import threading
from urllib.parse import quote

from ..models import UserError
from ._http import ContentBlocked, post_json, unexpected

BASE = "https://generativelanguage.googleapis.com/v1beta"
_BLOCK_FINISH = ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION")


def _block_reason(data) -> str:
    """Why Gemini refused, or "": a prompt blocked outright (promptFeedback.blockReason, no candidates)
    or a candidate stopped by a filter with no text."""
    if not isinstance(data, dict):
        return ""
    cands = data.get("candidates")
    if not cands:
        return str((data.get("promptFeedback") or {}).get("blockReason") or "")
    c = cands[0] if isinstance(cands[0], dict) else {}
    if c.get("finishReason") in _BLOCK_FINISH and not (c.get("content") or {}).get("parts"):
        return str(c["finishReason"])
    return ""


def complete(prompt: str, model: str, settings: dict, cancel: threading.Event | None = None) -> str:
    key = settings.get("gemini_key", "")
    if not key:
        raise UserError("Falta la clave de Gemini", "Añádela en Ajustes")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    # Key goes in a header, not the URL, so it never ends up in logs or tracebacks.
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    data = post_json(f"{BASE}/models/{quote(model, safe='')}:generateContent", headers, payload, "Gemini", model,
                     retry=True, cancel=cancel)
    reason = _block_reason(data)
    if reason:
        raise ContentBlocked(f"Gemini bloqueó el texto con su filtro de contenido ({reason})",
                             "Prueba otro proveedor de IA en Ajustes")
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError, AttributeError):
        raise unexpected("Gemini") from None
