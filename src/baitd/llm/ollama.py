"""Ollama through its own API when the "compatible server" address is an Ollama.

Ollama's OpenAI-compatible endpoint cannot set the context length, and Ollama's default (a few
thousand tokens) silently cuts a Clipping window of 20 minutes (~9,000 tokens) to its last part.
Its native /api/chat takes `options.num_ctx`, sized here from the prompt, plus `format: "json"`.
"""
from __future__ import annotations

import math
import threading
from urllib.parse import urlparse

import requests

from ..ffmpeg import JobCancelled
from ._http import decode_json, request, unexpected

LABEL = "Ollama"
TIMEOUT = 900  # seconds: a local model on a CPU can take minutes for one long prompt
CHARS_PER_TOKEN = 2.5  # conservative: a timestamped Spanish transcript measured ~2.7 with Gemini
REPLY_TOKENS = 2048
MIN_CTX, MAX_CTX = 4096, 32768
CANCEL_POLL_S = 0.5
_found: set[str] = set()  # roots already identified as Ollama


def root(base: str) -> str:
    """'http://host:11434/v1/' -> 'http://host:11434' (the native API lives at the root)."""
    base = base.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _probe(base_root: str) -> bool:
    try:
        resp = requests.get(f"{base_root}/api/version", timeout=3)
        return resp.status_code == 200 and "version" in resp.json()
    except (requests.RequestException, ValueError, TypeError):
        return False


def is_ollama(base: str) -> bool:
    """True when `base` is an Ollama of the user's own. Only a yes is remembered: a server that was off may
    start later. Ollama's cloud (ollama.com) keeps the /v1 path users already have working: it also answers
    /api/version, but its models size their own context and that path was never checked with a key."""
    host = (urlparse(base).hostname or "").lower()
    if host == "ollama.com" or host.endswith(".ollama.com"):
        return False
    r = root(base)
    if r in _found:
        return True
    if _probe(r):
        _found.add(r)
        return True
    return False


def context_for(prompt: str) -> int:
    """num_ctx that fits the prompt plus the reply, in steps of 1024, within MIN_CTX..MAX_CTX."""
    need = len(prompt) / CHARS_PER_TOKEN + REPLY_TOKENS
    return int(min(MAX_CTX, max(MIN_CTX, math.ceil(need / 1024) * 1024)))


def _cancellable(fn, cancel: threading.Event | None):
    """Run a blocking call in a thread and give up on it as soon as `cancel` is set: a local model can
    take many minutes on one request, and Cancelar must not wait for it (the request is left to finish)."""
    if cancel is None:
        return fn()
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # handed back to the caller's thread below
            box["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(CANCEL_POLL_S)
        if cancel.is_set():
            raise JobCancelled()
    if "error" in box:
        raise box["error"]
    return box["value"]


def complete(prompt: str, model: str, base: str, key: str = "", cancel: threading.Event | None = None) -> str:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"num_ctx": context_for(prompt), "temperature": 0.8},
    }
    def call():
        return request("post", f"{root(base)}/api/chat", LABEL, model, headers=headers, json=payload,
                       connect_fix="¿Está abierto Ollama?",
                       not_found_fix=f"Descárgalo con: ollama pull {model}",
                       retry=True, cancel=cancel, timeout=TIMEOUT)

    data = decode_json(_cancellable(call, cancel), LABEL)
    try:
        return data["message"]["content"] or ""
    except (KeyError, TypeError):
        raise unexpected(LABEL) from None
