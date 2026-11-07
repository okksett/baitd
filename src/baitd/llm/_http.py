from __future__ import annotations

import requests

from ..models import UserError

TIMEOUT = 120  # seconds; local models can be slow
# Bodies that mean "bad key" even though the status is 400 (Gemini, Pixabay).
_BAD_KEY_400 = ("API_KEY_INVALID", "Invalid or missing API key")


class AuthError(UserError):
    """The provider rejected the key (callers stop asking that provider)."""


class NotFoundError(UserError):
    """The provider has no such model / voice (HTTP 404)."""


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _of(provider: str) -> str:
    """Spanish "de" + provider, contracting "de el" to "del" ("el servidor compatible")."""
    return f"del {provider[3:]}" if provider.startswith("el ") else f"de {provider}"


def request(method: str, url: str, provider: str, model: str = "", *, noun: str = "el modelo",
            connect_fix: str | None = None, not_found_fix: str | None = None,
            **kwargs) -> requests.Response:
    """Send an HTTP request (requests.<method>) and return the 200 response; map failures to UserError.

    `noun` names what `model` is ("el modelo", "la voz"); `connect_fix` / `not_found_fix` replace
    the generic advice for providers where it would mislead (a local server, a base URL)."""
    try:
        resp = getattr(requests, method)(url, timeout=TIMEOUT, **kwargs)
    except requests.Timeout:
        raise UserError(f"{_cap(provider)} tardó demasiado en responder",
                        "Inténtalo de nuevo o prueba otro modelo") from None
    except requests.ConnectionError:
        raise UserError(f"Sin conexión con {provider}",
                        connect_fix or "Comprueba tu conexión a internet") from None
    except requests.RequestException as e:
        raise UserError(f"Error de red con {provider} ({type(e).__name__})", "Inténtalo de nuevo") from None

    status = resp.status_code
    if status == 200:
        return resp
    if status in (401, 403) or (status == 400 and any(m in resp.text for m in _BAD_KEY_400)):
        raise AuthError(f"La clave {_of(provider)} no es válida", "Revísala en Ajustes")
    if status == 404:
        raise NotFoundError(f"{_cap(noun)} '{model}' no existe en {provider}",
                            not_found_fix or f"Cambia {noun} en el perfil")
    if status == 429:
        raise UserError(f"{_cap(provider)} alcanzó el límite de peticiones o de cuota",
                        "Espera un momento o revisa tu plan")
    raise UserError(f"{_cap(provider)} respondió con el error {status}", "Inténtalo de nuevo más tarde")


def decode_json(resp: requests.Response, provider: str) -> dict:
    try:
        return resp.json()
    except ValueError:
        raise UserError(f"{_cap(provider)} devolvió una respuesta ilegible", "Inténtalo de nuevo") from None


def post_json(url: str, headers: dict, payload: dict, provider: str, model: str, **kwargs) -> dict:
    """POST JSON and return the decoded reply, mapping failures to UserError."""
    return decode_json(request("post", url, provider, model, headers=headers, json=payload, **kwargs), provider)


def unexpected(provider: str) -> UserError:
    return UserError(f"{_cap(provider)} devolvió una respuesta inesperada", "Inténtalo de nuevo o prueba otro modelo")
