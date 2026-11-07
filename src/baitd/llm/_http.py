from __future__ import annotations

import requests

from ..models import UserError

TIMEOUT = 120  # seconds; local models can be slow


def post_json(url: str, headers: dict, payload: dict, provider: str, model: str) -> dict:
    """POST JSON and return the decoded reply, mapping failures to UserError."""
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    except requests.Timeout:
        raise UserError(f"{provider} tardó demasiado en responder",
                        "Inténtalo de nuevo o prueba otro modelo") from None
    except requests.ConnectionError:
        raise UserError(f"Sin conexión con {provider}", "Comprueba tu conexión a internet") from None
    except requests.RequestException as e:
        raise UserError(f"Error de red con {provider}: {e}", "Inténtalo de nuevo") from None

    status = resp.status_code
    if status == 200:
        try:
            return resp.json()
        except ValueError:
            raise UserError(f"{provider} devolvió una respuesta ilegible", "Inténtalo de nuevo") from None
    # Gemini reports a bad key as 400 + API_KEY_INVALID.
    if status in (401, 403) or (status == 400 and "API_KEY_INVALID" in resp.text):
        raise UserError(f"La clave de {provider} no es válida", "Revísala en Ajustes")
    if status == 404:
        raise UserError(f"El modelo '{model}' no existe en {provider}", "Cambia el modelo en el perfil")
    if status == 429:
        raise UserError(f"{provider} alcanzó el límite de peticiones o de cuota",
                        "Espera un momento o revisa tu plan")
    raise UserError(f"{provider} respondió con el error {status}", "Inténtalo de nuevo más tarde")


def unexpected(provider: str) -> UserError:
    return UserError(f"{provider} devolvió una respuesta inesperada", "Inténtalo de nuevo o prueba otro modelo")
