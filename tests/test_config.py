import os
import sys

import pytest

from baitd.config import (
    default_profile, delete_profile, list_profiles, load_settings,
    save_profile, save_settings, slugify, data_dir,
)
from baitd.models import JobCancelled, Profile, SubtitleStyle, UserError
from baitd import ffmpeg


def test_settings_roundtrip(tmp_data):
    save_settings({"pexels_key": "k"})
    assert load_settings()["pexels_key"] == "k"


def test_settings_defaults_when_missing(tmp_data):
    s = load_settings()
    assert s["pexels_key"] == "" and s["ui_lang"] and "theme" in s


def test_data_dir_env_override(tmp_data):
    assert data_dir() == tmp_data


def test_profiles_crud(tmp_data):
    p = default_profile()
    p.id, p.name = "x", "Datos curiosos"
    save_profile(p)
    assert [q.name for q in list_profiles()] == ["Datos curiosos"]
    p.name = "Otro"
    save_profile(p)
    assert [q.name for q in list_profiles()] == ["Otro"]
    delete_profile("x")
    assert list_profiles() == []


def test_profile_roundtrip_json():
    p = default_profile()
    assert Profile.from_dict(p.to_dict()) == p
    assert isinstance(Profile.from_dict(p.to_dict()).subtitle, SubtitleStyle)


def test_default_profile_values():
    p = default_profile()
    assert (p.language, p.aspect, p.fps, p.seconds) == ("es", "9:16", 30, 45)
    assert (p.llm_provider, p.tts_provider, p.voice, p.rate) == ("gemini", "edge", "es-ES-ElviraNeural", 0)
    assert (p.subtitle.preset, p.music, p.music_volume) == ("caja_azul", "lofi", 0.15)


def test_slugify_unicode():
    assert slugify("¿Por qué el café 🔥 nos despierta?") == "por-que-el-cafe-nos-despierta"


def test_slugify_max_len():
    assert len(slugify("a" * 100)) == 40


def test_user_error_and_reexport():
    e = UserError("Falta la clave de Pexels", "Ajustes")
    assert (e.cause, e.fix) == ("Falta la clave de Pexels", "Ajustes")
    assert str(e) == "Falta la clave de Pexels → Ajustes"
    assert JobCancelled is ffmpeg.JobCancelled


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_settings_file_not_world_readable_on_posix(tmp_data):
    save_settings({"pexels_key": "k"})
    assert os.stat(tmp_data / "settings.json").st_mode & 0o777 == 0o600
