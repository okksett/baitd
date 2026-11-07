from __future__ import annotations

import json
import os
import re
import tempfile
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
    "cache_max_gb": 3,  # clip cache cap in GB (ruling R30); not in the UI, editable in settings.json
}


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


def load_settings() -> dict:
    data = _read_json(data_dir() / "settings.json", {}, dict)
    return {**SETTINGS_DEFAULTS, **data}


def save_settings(d: dict) -> None:
    """Merge d over the stored settings (partial updates keep other keys)."""
    _write_json(data_dir() / "settings.json", {**load_settings(), **d})


def default_profile() -> Profile:
    return Profile(
        id="default", name="Por defecto", language="es", aspect="9:16", fps=30,
        seconds=45, llm_provider="gemini", llm_model="", tone="",
        tts_provider="edge", voice="es-ES-ElviraNeural", rate=0,
        subtitle=replace(PRESETS["caja_azul"]),
        music="lofi", music_volume=0.15,
    )


def list_profiles() -> list[Profile]:
    raw = _read_json(data_dir() / "profiles.json", [], list)
    out = []
    for d in raw:
        try:
            out.append(Profile.from_dict(d))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return out


def save_profile(p: Profile) -> None:
    current = list_profiles()
    if any(q.id == p.id for q in current):
        current = [p if q.id == p.id else q for q in current]
    else:
        current.append(p)
    _write_json(data_dir() / "profiles.json", [q.to_dict() for q in current])


def delete_profile(id: str) -> None:
    keep = [q.to_dict() for q in list_profiles() if q.id != id]
    _write_json(data_dir() / "profiles.json", keep)


def slugify(text: str, max_len: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].strip("-") or "video"
