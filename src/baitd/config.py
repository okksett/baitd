from __future__ import annotations

import json
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
}


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
    return {**SETTINGS_DEFAULTS, **raw_settings()}


def save_settings(d: dict) -> None:
    """Merge d over the stored settings (partial updates keep other keys; defaults stay implicit)."""
    with _LOCK:
        _write_json(data_dir() / "settings.json", {**raw_settings(), **d})


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
