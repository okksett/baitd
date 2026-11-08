from __future__ import annotations

from ..models import UserError
from ._http import post_json, unexpected

OPENAI_BASE = "https://api.openai.com/v1"


def complete(prompt: str, model: str, settings: dict, provider: str = "openai") -> str:
    """Chat completion against OpenAI or any OpenAI-compatible server (provider 'compat')."""
    headers = {"Content-Type": "application/json"}
    if provider == "openai":
        label, base, key = "OpenAI", OPENAI_BASE, settings.get("openai_key", "")
        if not key:
            raise UserError("Falta la clave de OpenAI", "Añádela en Ajustes")
    else:
        label, base, key = "el servidor compatible", settings.get("compat_base_url", "").rstrip("/"), settings.get("compat_key", "")
        if not base:
            raise UserError("Falta la dirección del servidor compatible", "Indícala en Ajustes")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.8}
    if provider == "openai":
        payload["response_format"] = {"type": "json_object"}
    hints = {} if provider == "openai" else {  # a local server (Ollama), not "the internet"
        "connect_fix": "¿Está abierto Ollama / el servidor?",
        "not_found_fix": "Revisa la URL (¿termina en /v1?) o el modelo",
    }
    data = post_json(f"{base}/chat/completions", headers, payload, label, model, **hints)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise unexpected(label) from None
