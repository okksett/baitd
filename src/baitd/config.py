from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from platformdirs import user_data_dir

from .captions import PRESETS
from .models import Profile

SETTINGS_DEFAULTS: dict = {
    "openai_key": "", "gemini_key": "", "elevenlabs_key": "", "pexels_key": "",
    "pixabay_key": "", "compat_base_url": "", "compat_key": "", "output_dir": "",
    "ui_lang": "es", "theme": "dark",
    "llm_provider": "gemini",  # AI text provider (a key of llm.DEFAULT_MODELS)
    "llm_model": "",  # "" = the provider's default model
    "cache_max_gb": 3,  # clip cache cap in GB (ruling R30); not in the UI, editable in settings.json
    "check_updates": True,  # look for a newer Baitd release when the app opens
    "keep_sources": False,  # Clipping: keep downloaded/uploaded videos and analyses after a run
    "yt_cookies_browser": "",  # Clipping: read the YouTube session from this browser (sources.COOKIE_BROWSERS)
    "clip_max_height": 1440,  # Clipping: max download height (sources.QUALITIES)
    "yt_cookies_file": "",  # Clipping: path to an exported cookies.txt; wins over the browser
    "live_hotkey": "F9",  # En directo: the global key that marks a moment
    "live_before_s": 45,  # En directo: seconds kept before a mark (10-120)
    "live_after_s": 15,  # En directo: seconds kept after a mark (5-60)
    "live_ai_per_hour": 6,  # En directo: AI requests per hour for automatic moments (0-30)
    "live_max_height": 1080,  # En directo: capture quality, 720 or 1080
}


# API keys live in the OS credential store (Windows Credential Manager, macOS Keychain, Secret Service);
# settings.json keeps them only where no store works, as before.
SECRET_KEYS = ("openai_key", "gemini_key", "elevenlabs_key", "pexels_key", "pixabay_key", "compat_key")
_SERVICE = "Baitd"
log = logging.getLogger(__name__)
_secrets: dict[str, str] | None = None  # what the store holds, read once per run


def _vault():
    """The OS credential store, or None (no usable backend, or BAITD_NO_KEYRING set: the tests)."""
    if os.environ.get("BAITD_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail
        backend = keyring.get_keyring()
        return None if isinstance(backend, fail.Keyring) or backend.priority <= 0 else backend
    except Exception as e:  # no backend on this system (e.g. Linux without a Secret Service)
        log.info("Sin almacén de credenciales del sistema (%s); las claves quedan en settings.json",
                 type(e).__name__)
        return None


def _stored_secrets(vault) -> dict[str, str]:
    global _secrets
    if _secrets is None:
        found = {}
        for key in SECRET_KEYS:
            try:
                found[key] = vault.get_password(_SERVICE, key) or ""
            except Exception as e:
                log.warning("No se pudo leer %s del almacén de credenciales (%s)", key, type(e).__name__)
                found[key] = ""
        _secrets = found
    return _secrets


def _store_secret(vault, key: str, value: str) -> bool:
    """True when the store now holds `value` ("" deletes it)."""
    try:
        if value:
            vault.set_password(_SERVICE, key, value)
        else:
            try:
                vault.delete_password(_SERVICE, key)
            except Exception:  # nothing stored under that name
                pass
    except Exception as e:
        log.warning("No se pudo guardar %s en el almacén de credenciales (%s); queda en settings.json",
                    key, type(e).__name__)
        return False
    _stored_secrets(vault)[key] = value
    return True


# Serialises read-modify-write of settings.json / profiles.json (waitress serves on several
# threads); reads take it too, so os.replace never races an open reader on Windows.
_LOCK = threading.RLock()


def data_dir() -> Path:
    env = os.environ.get("BAITD_DATA")
    d = Path(env) if env else Path(user_data_dir("Baitd"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, obj) -> None:
    # mkstemp creates the file 0600 and with a unique name in the same dir.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _quarantine(path: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{n}")
        n += 1
    try:
        os.replace(path, target)
    except OSError:
        pass


def _read_json(path: Path, default, kind: type):
    """Read JSON of the expected type; quarantine unreadable/wrong-shaped files."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (ValueError, OSError):  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        _quarantine(path)
        return default
    if not isinstance(data, kind):
        _quarantine(path)
        return default
    return data


def raw_settings() -> dict:
    """Only what was saved, without defaults (e.g. no "theme" until the user picks one)."""
    with _LOCK:
        return _read_json(data_dir() / "settings.json", {}, dict)


def load_settings() -> dict:
    with _LOCK:
        s = {**SETTINGS_DEFAULTS, **raw_settings()}
        vault = _vault()
        if vault is not None:
            for key, value in _stored_secrets(vault).items():
                if value and not s.get(key):  # a key still in the file (no store before) wins
                    s[key] = value
        return s


def save_settings(d: dict) -> None:
    """Merge d over the stored settings (partial updates keep other keys; defaults stay implicit).
    API keys go to the OS credential store when there is one, and the file keeps "" for them."""
    with _LOCK:
        d = dict(d)
        vault = _vault()
        if vault is not None:
            for key in SECRET_KEYS:
                if key in d and isinstance(d[key], str) and _store_secret(vault, key, d[key]):
                    d[key] = ""
        _write_json(data_dir() / "settings.json", {**raw_settings(), **d})


def migrate_secrets() -> None:
    """Move API keys saved in settings.json (before 0.6.1) into the OS credential store."""
    with _LOCK:
        raw = raw_settings()
        found = {k: raw[k] for k in SECRET_KEYS if isinstance(raw.get(k), str) and raw[k]}
        if found and _vault() is not None:
            save_settings(found)


def migrate_llm_settings() -> None:
    """One time (0.2 -> 0.3): the AI provider/model moved from each profile to the settings.
    Takes them from the first profile with a model (else the first profile)."""
    with _LOCK:
        if "llm_provider" in raw_settings():
            return
        raw = _read_json(data_dir() / "profiles.json", [], list)
        entries = [d for d in raw if isinstance(d, dict)]
        if not entries:
            return
        pick = next((d for d in entries if isinstance(d.get("llm_model"), str) and d["llm_model"].strip()),
                    entries[0])
        provider, model = pick.get("llm_provider"), pick.get("llm_model")
        from .llm import DEFAULT_MODELS  # late: llm imports models, not config

        update: dict = {}
        if isinstance(provider, str) and provider in DEFAULT_MODELS:
            update["llm_provider"] = provider
        if isinstance(model, str) and model.strip():
            update["llm_model"] = model.strip()[:80]
        if update:
            update.setdefault("llm_provider", SETTINGS_DEFAULTS["llm_provider"])
            save_settings(update)


def default_profile() -> Profile:
    return Profile(
        id="default", name="Por defecto", language="es", aspect="9:16", fps=30,
        seconds=45, tone="",
        tts_provider="edge", voice="es-ES-ElviraNeural", rate=0,
        subtitle=replace(PRESETS["caja_azul"]),
        music="lofi", music_volume=0.15,
    )


def list_profiles() -> list[Profile]:
    with _LOCK:
        raw = _read_json(data_dir() / "profiles.json", [], list)
    out = []
    for d in raw:
        try:
            out.append(Profile.from_dict(d))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return out


def save_profile(p: Profile) -> None:
    with _LOCK:
        current = list_profiles()
        if any(q.id == p.id for q in current):
            current = [p if q.id == p.id else q for q in current]
        else:
            current.append(p)
        _write_json(data_dir() / "profiles.json", [q.to_dict() for q in current])


def delete_profile(id: str) -> None:
    with _LOCK:
        keep = [q.to_dict() for q in list_profiles() if q.id != id]
        _write_json(data_dir() / "profiles.json", keep)


def slugify(text: str, max_len: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].strip("-") or "video"
