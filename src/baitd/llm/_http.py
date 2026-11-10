from __future__ import annotations

import math
import threading
import time

import requests

from ..ffmpeg import JobCancelled
from ..models import UserError

TIMEOUT = 120  # seconds; local models can be slow
# Bodies that mean "bad key" even though the status is 400 (Gemini, Pixabay).
_BAD_KEY_400 = ("API_KEY_INVALID", "Invalid or missing API key")
# Busy-provider statuses are retried after these waits (seconds): 3 retries, 4 attempts in all.
RETRY_STATUSES = (429, 500, 502, 503, 504)
RETRY_DELAYS = (5, 15, 30)
# A 429 that asks to wait longer than this (a daily quota, ~24 h) is not worth waiting for in a job.
MAX_WAIT_S = 90


class AuthError(UserError):
    """The provider rejected the key (callers stop asking that provider)."""


class NotFoundError(UserError):
    """The provider has no such model / voice (HTTP 404)."""


class ContentBlocked(UserError):
    """The provider's content filter refused the text (callers that split a long text skip the piece)."""


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _of(provider: str) -> str:
    """Spanish "de" + provider, contracting "de el" to "del" ("el servidor compatible")."""
    return f"del {provider[3:]}" if provider.startswith("el ") else f"de {provider}"


def _sleep(seconds: float, cancel: threading.Event | None = None) -> None:
    """Wait `seconds` in 1-second slices; raise JobCancelled as soon as `cancel` is set.
    Wraps time.sleep so tests can monkeypatch it."""
    left = seconds
    while left > 0:
        if cancel is not None and cancel.is_set():
            raise JobCancelled()
        step = min(1, left)
        time.sleep(step)
        left -= step
    if cancel is not None and cancel.is_set():
        raise JobCancelled()


def _seconds(v) -> float | None:
    """'37s' / '1.5s' (google.rpc.RetryInfo) or '20' (Retry-After) -> seconds; None when unreadable."""
    try:
        return float(str(v).strip().rstrip("s"))
    except (TypeError, ValueError):
        return None


def _quota_hint(resp: requests.Response) -> tuple[float | None, bool, str]:
    """Read a 429: (seconds the provider asks to wait or None, True when retrying soon cannot help,
    a short Spanish detail such as "20 peticiones al día en el plan gratis" or "").
    Gemini sends google.rpc QuotaFailure (quotaId '...PerDay...') and RetryInfo (retryDelay '84005s');
    OpenAI sends error.code 'insufficient_quota' when the balance is gone."""
    delay = _seconds(resp.headers.get("Retry-After")) if getattr(resp, "headers", None) else None
    exhausted, detail = False, ""
    try:
        err = resp.json().get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    if not isinstance(err, dict):
        err = {}
    if err.get("code") == "insufficient_quota" or err.get("type") == "insufficient_quota":
        exhausted = True
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        if d.get("@type", "").endswith("RetryInfo"):
            delay = _seconds(d.get("retryDelay"))
        for v in d.get("violations") or []:
            qid = str(v.get("quotaId", "")) if isinstance(v, dict) else ""
            if "PerDay" in qid:
                exhausted = True
                n = str(v.get("quotaValue", "")).strip()
                if n.isdigit():
                    detail = f"{n} peticiones al día" + (" en el plan gratis" if "FreeTier" in qid else "")
    if delay is not None and delay > MAX_WAIT_S:
        exhausted = True
    return delay, exhausted, detail


def request(method: str, url: str, provider: str, model: str = "", *, noun: str = "el modelo",
            connect_fix: str | None = None, not_found_fix: str | None = None,
            retry: bool = False, cancel: threading.Event | None = None,
            **kwargs) -> requests.Response:
    """Send an HTTP request (requests.<method>) and return the 200 response; map failures to UserError.

    `noun` names what `model` is ("el modelo", "la voz"); `connect_fix` / `not_found_fix` replace
    the generic advice for providers where it would mislead (a local server, a base URL).
    With `retry=True` (AI text generation only) busy statuses (RETRY_STATUSES) are retried after
    RETRY_DELAYS and `cancel` interrupts the waits; otherwise there is a single attempt.
    A 429 waits at least what the provider asks for (up to MAX_WAIT_S) and is not retried at all
    when the quota is daily or the balance is gone."""
    delays = list(RETRY_DELAYS) if retry else []
    delay = 0.0
    while True:
        if delay:
            _sleep(delay, cancel)
            if cancel is not None and cancel.is_set():
                raise JobCancelled()
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
        if not delays or resp.status_code not in RETRY_STATUSES:
            break
        delay = delays.pop(0)
        if resp.status_code == 429:
            asked, exhausted, _ = _quota_hint(resp)
            if exhausted:
                break
            if asked is not None:
                delay = max(delay, math.ceil(asked) + 1)

    status = resp.status_code
    if status == 200:
        return resp
    if status in (401, 403) or (status == 400 and any(m in resp.text for m in _BAD_KEY_400)):
        raise AuthError(f"La clave {_of(provider)} no es válida", "Revísala en Ajustes")
    if status == 404:
        raise NotFoundError(f"{_cap(noun)} '{model}' no existe en {provider}",
                            not_found_fix or ("Cambia el modelo en Ajustes" if noun == "el modelo" else f"Cambia {noun} en el perfil"))
    if status == 429:
        _, exhausted, detail = _quota_hint(resp)
        if exhausted:
            where = f" para el modelo '{model}'" if model else ""
            extra = f" ({detail})" if detail else ""
            raise UserError(f"Se acabó la cuota de {provider}{where}{extra}",
                            "Elige otro modelo en Ajustes, espera a que se renueve (la diaria, mañana) "
                            "o revisa tu plan")
        raise UserError(f"{_cap(provider)} alcanzó el límite de peticiones o de cuota",
                        "Espera un momento o revisa tu plan")
    if retry and status >= 500:
        raise UserError(f"{_cap(provider)} está saturado (error {status})",
                        "Espera unos minutos y pulsa Reintentar, o elige otro modelo en Ajustes")
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
