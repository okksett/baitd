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


def test_migrate_copies_profiles_settings_music_once(tmp_path):
    from baitd import config
    old, new = tmp_path / "Baitd", tmp_path / "Baitd"
    (old / "musica").mkdir(parents=True)
    (old / "cache").mkdir()
    (old / "settings.json").write_text('{"openai_key": "sk-x"}', encoding="utf-8")
    (old / "profiles.json").write_text("[]", encoding="utf-8")
    (old / "musica" / "a.mp3").write_bytes(b"x")
    (old / "queue.json").write_text("[]", encoding="utf-8")
    (old / "cache" / "c.bin").write_bytes(b"x")
    assert config.migrate_legacy_data(old, new) is True
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"openai_key": "sk-x"}'
    assert (new / "profiles.json").exists()
    assert (new / "musica" / "a.mp3").exists()
    assert not (new / "queue.json").exists() and not (new / "cache").exists()
    assert config.migrate_legacy_data(old, new) is False  # new already exists -> no-op


def test_migrate_noop_without_old(tmp_path):
    from baitd import config
    assert config.migrate_legacy_data(tmp_path / "Baitd", tmp_path / "Baitd") is False
    assert not (tmp_path / "Baitd").exists()


def test_data_dir_prefers_baitd_then_legacy_env(tmp_path, monkeypatch):
    from baitd import config
    monkeypatch.delenv("BAITD_DATA", raising=False)
    monkeypatch.setenv("BAITD_DATA", str(tmp_path / "legacy"))
    assert config.data_dir() == tmp_path / "legacy"
    monkeypatch.setenv("BAITD_DATA", str(tmp_path / "new"))
    assert config.data_dir() == tmp_path / "new"


def test_migrate_rewrites_music_paths(tmp_path):
    import json
    from baitd import audio, config
    old, new = tmp_path / "Baitd", tmp_path / "Baitd"
    (old / "musica").mkdir(parents=True)
    (old / "musica" / "mia.mp3").write_bytes(b"x")
    bundled = next((audio.MUSIC_DIR / "lofi").glob("*.mp3"))
    old_pkg = tmp_path / "site-packages" / "baitd" / "assets" / "music"

    def prof(pid, music):
        return {**default_profile().to_dict(), "id": pid, "name": pid, "music": music}

    profiles = [prof("a", str(old_pkg / "lofi" / bundled.name)),     # old bundled track -> new bundled path
                prof("b", str(old / "musica" / "mia.mp3")),           # old user track -> new musica/
                prof("c", str(old_pkg / "lofi" / "borrada.mp3")),     # bundled track that no longer exists
                prof("d", str(tmp_path / "otra" / "x.mp3")),          # anything else unresolvable
                prof("e", "epico"), prof("f", "none"), prof("g", str(bundled))]  # already valid: unchanged
    (old / "profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
    assert config.migrate_legacy_data(old, new) is True
    music = {p["id"]: p["music"] for p in json.loads((new / "profiles.json").read_text(encoding="utf-8"))}
    default = default_profile().music
    assert music == {"a": str(bundled), "b": str(new / "musica" / "mia.mp3"), "c": default, "d": default,
                     "e": "epico", "f": "none", "g": str(bundled)}


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
