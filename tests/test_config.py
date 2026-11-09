import os
import sys

import pytest

from baitd.config import (
    default_profile, delete_profile, list_profiles, load_settings,
    save_profile, save_settings, slugify, data_dir,
)
from baitd.models import JobCancelled, Profile, SubtitleStyle, UserError
from baitd import config, ffmpeg


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
    assert (p.tts_provider, p.voice, p.rate) == ("edge", "es-ES-ElviraNeural", 0)
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


def _corrupt_files(d, name):
    return list(d.glob(name + ".corrupt-*"))


def test_save_settings_merges_partial(tmp_data):
    save_settings({"pexels_key": "k"})
    save_settings({"theme": "light"})
    s = load_settings()
    assert s["pexels_key"] == "k" and s["theme"] == "light"


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\x00garbage", b"[1, 2]"])
def test_corrupt_settings_quarantined(tmp_data, content):
    (tmp_data / "settings.json").write_bytes(content)
    assert load_settings()["pexels_key"] == ""
    assert len(_corrupt_files(tmp_data, "settings.json")) == 1
    assert not (tmp_data / "settings.json").exists()
    save_settings({"pexels_key": "k"})  # does not destroy the quarantined copy
    assert _corrupt_files(tmp_data, "settings.json")[0].read_bytes() == content


@pytest.mark.parametrize("content", [b"{oops", b"\xff\xfe\x00garbage", b'{"a": 1}'])
def test_corrupt_profiles_quarantined(tmp_data, content):
    (tmp_data / "profiles.json").write_bytes(content)
    assert list_profiles() == []
    assert len(_corrupt_files(tmp_data, "profiles.json")) == 1


def test_bad_profile_entry_skipped_good_kept(tmp_data):
    import json
    good = default_profile().to_dict()
    good.update(id="g", name="Bueno", future_field=1)  # unknown key ignored
    del good["tone"]  # missing optional filled from defaults
    bad_missing = {"name": "sin id"}
    bad_type = {**default_profile().to_dict(), "id": "b", "name": "B", "subtitle": "oops"}
    (tmp_data / "profiles.json").write_text(
        json.dumps([bad_missing, good, bad_type, 5]), encoding="utf-8")
    assert [p.name for p in list_profiles()] == ["Bueno"]
    assert not _corrupt_files(tmp_data, "profiles.json")


def test_slugify_all_emoji_fallback():
    assert slugify("🔥🔥") == "video"


def test_data_dir_from_env(tmp_path, monkeypatch):
    from baitd import config
    monkeypatch.setenv("BAITD_DATA", str(tmp_path / "datos"))
    assert config.data_dir() == tmp_path / "datos" and (tmp_path / "datos").is_dir()


def test_bad_clipping_block_keeps_the_profile(tmp_data, caplog):
    import json
    from baitd.clip.models import ClipProfile
    bad = {**default_profile().to_dict(), "id": "p", "name": "Mío", "tone": "serio", "clipping": {"count": 99}}
    (tmp_data / "profiles.json").write_text(json.dumps([bad]), encoding="utf-8")
    with caplog.at_level("WARNING"):
        profiles = list_profiles()
    assert [(p.name, p.tone) for p in profiles] == [("Mío", "serio")]
    assert profiles[0].clipping == ClipProfile()
    assert any("clipping" in r.getMessage().lower() for r in caplog.records)


def _write_profiles(tmp_data, *entries):
    import json
    (tmp_data / "profiles.json").write_text(json.dumps(list(entries)), encoding="utf-8")


def test_migrate_llm_settings_moves_profile_model_once(tmp_data):
    _write_profiles(tmp_data, {"id": "a", "name": "A", "llm_provider": "gemini", "llm_model": ""},
                    {"id": "b", "name": "B", "llm_provider": "openai", "llm_model": "gpt-x"})
    config.migrate_llm_settings()
    s = config.raw_settings()
    assert (s["llm_provider"], s["llm_model"]) == ("openai", "gpt-x")  # first profile WITH a model
    _write_profiles(tmp_data, {"id": "a", "name": "A", "llm_provider": "compat", "llm_model": "otro"})
    config.migrate_llm_settings()
    assert (config.raw_settings()["llm_provider"], config.raw_settings()["llm_model"]) == ("openai", "gpt-x")


def test_migrate_llm_settings_uses_first_profile_without_models(tmp_data):
    _write_profiles(tmp_data, {"id": "a", "name": "A", "llm_provider": "compat", "llm_model": ""})
    config.migrate_llm_settings()
    assert config.raw_settings() == {"llm_provider": "compat"}


def test_migrate_llm_settings_ignores_invalid_provider(tmp_data):
    _write_profiles(tmp_data, {"id": "a", "name": "A", "llm_provider": "zzz", "llm_model": "m"})
    config.migrate_llm_settings()
    assert config.raw_settings() == {"llm_provider": "gemini", "llm_model": "m"}


def test_migrate_llm_settings_noop_without_profiles(tmp_data):
    config.migrate_llm_settings()
    assert config.raw_settings() == {}
    _write_profiles(tmp_data)
    config.migrate_llm_settings()
    assert config.raw_settings() == {}


def test_old_profile_with_llm_fields_still_loads():
    d = default_profile().to_dict()
    d.update(llm_provider="openai", llm_model="gpt-x")
    p = Profile.from_dict(d)
    assert p == default_profile() and not hasattr(p, "llm_model")
