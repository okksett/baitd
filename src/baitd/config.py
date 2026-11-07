from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from platformdirs import user_data_dir

from .models import Profile, SubtitleStyle

SETTINGS_DEFAULTS: dict = {
    "openai_key": "", "gemini_key": "", "elevenlabs_key": "", "pexels_key": "",
    "pixabay_key": "", "compat_base_url": "", "compat_key": "", "output_dir": "",
    "ui_lang": "es", "theme": "dark",
}


def data_dir() -> Path:
    env = os.environ.get("BAITD_DATA")
    d = Path(env) if env else Path(user_data_dir("Baitd"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_settings() -> dict:
    data = _read_json(data_dir() / "settings.json", {})
    return {**SETTINGS_DEFAULTS, **(data if isinstance(data, dict) else {})}


def save_settings(d: dict) -> None:
    path = data_dir() / "settings.json"
    _write_json(path, d)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def default_profile() -> Profile:
    return Profile(
        id="default", name="Por defecto", language="es", aspect="9:16", fps=30,
        seconds=45, llm_provider="gemini", llm_model="", tone="",
        tts_provider="edge", voice="es-ES-ElviraNeural", rate=0,
        subtitle=SubtitleStyle(
            preset="caja_azul", font="Montserrat ExtraBold", size=72,
            color="#FFFFFF", highlight="#0046DE", outline=0, box=True,
            position="bottom",
        ),
        music="lofi", music_volume=0.15,
    )


def list_profiles() -> list[Profile]:
    raw = _read_json(data_dir() / "profiles.json", [])
    return [Profile.from_dict(d) for d in raw]


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
    return slug[:max_len].strip("-")
