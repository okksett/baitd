import dataclasses
import logging
import os
import shutil
import threading
from datetime import date

import pytest

from baitd import pipeline
from baitd.config import default_profile
from baitd.ffmpeg import probe
from baitd.models import JobCancelled, Media, Scene, Script, UserError, Voiceover, Word

SCRIPT = Script(
    title="Leer cambia tu vida", hook="Leer te cambia.",
    scenes=[Scene("Leer te cambia. Es un hecho.", ["book", "reading"]),
            Scene("Abre un libro hoy mismo ya.", ["library", "pages"])],
    description="Beneficios de leer", hashtags=["lectura", "#libros"], language="es")


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, key, idx, total, pct):
        self.calls.append((key, idx, total, pct))


@pytest.fixture
def prof():
    return dataclasses.replace(default_profile(), music="none")


@pytest.fixture
def fakes(monkeypatch, media_dir, tmp_data):
    state = {"llm": 0, "counter": 0, "used_seen": []}

    def gen(topic, profile, settings):
        state["llm"] += 1
        return SCRIPT

    def synth(script, profile, settings, workdir):
        shutil.copy2(media_dir / "voice.wav", workdir / "voice.wav")
        words = [Word(f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(12)]
        return Voiceover(workdir / "voice.wav", 6.0, words, [(0.0, 2.5), (2.5, 6.0)])

    def find(keywords, need, min_seconds, w, h, used, settings):
        state["used_seen"].append(set(used))
        out = []
        for _ in range(need):
            state["counter"] += 1
            out.append(Media(f"m{state['counter']}", "test", "video", "", 720, 1280, 4.0))
        return out

    def download(m):
        return media_dir / "v_port.mp4"

    monkeypatch.setattr(pipeline.llm, "generate_script", gen)
    monkeypatch.setattr(pipeline.tts, "synthesize", synth)
    monkeypatch.setattr(pipeline, "find_media", find)
    monkeypatch.setattr(pipeline.cache, "download", download)
    return state


def test_run_job_creates_outputs(tmp_data, fakes, prof):
    d = pipeline.run_job("los beneficios de leer", prof, {}, None, threading.Event(), Rec())
    assert {p.name for p in d.iterdir()} == {"video.mp4", "guion.txt", "publicacion.txt"}
    assert d.parent == tmp_data / "salidas"
    i = probe(d / "video.mp4")
    assert (i["width"], i["height"]) == (1080, 1920) and abs(i["duration"] - 6.0) < 0.2
    guion = (d / "guion.txt").read_text(encoding="utf-8")
    # M10: title then the scenes; the hook is scene 1's first sentence, so it is printed once
    assert guion == "Leer cambia tu vida\n\nLeer te cambia. Es un hecho.\n\nAbre un libro hoy mismo ya.\n"
    assert guion.count("Leer te cambia.") == 1
    assert (d / "publicacion.txt").read_text(encoding="utf-8") == (
        "Leer cambia tu vida\n\nBeneficios de leer\n\n#lectura #libros\n")


def test_progress_order(tmp_data, fakes, prof):
    rec = Rec()
    pipeline.run_job("tema", prof, {}, None, threading.Event(), rec)
    keys = []
    for k, *_ in rec.calls:
        if not keys or keys[-1] != k:
            keys.append(k)
    assert keys == ["script", "voice", "clips", "edit", "captions", "render"]
    assert all(t == 6 for _, _, t, _ in rec.calls)
    assert rec.calls[0] == ("script", 1, 6, 0.0) and rec.calls[-1] == ("render", 6, 6, 100.0)


def test_uses_given_script(tmp_data, fakes, prof):
    pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec())
    assert fakes["llm"] == 0


def test_shared_used_set_across_scenes(tmp_data, fakes, prof):
    pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec())
    seen = fakes["used_seen"]
    assert len(seen) == 2 and seen[0] == set() and len(seen[1]) >= 1


def test_unique_folder_names(tmp_data, fakes, prof):
    a = pipeline.run_job("mismo tema", prof, {}, SCRIPT, threading.Event(), Rec())
    b = pipeline.run_job("mismo tema", prof, {}, SCRIPT, threading.Event(), Rec())
    assert a.name == f"{date.today().isoformat()}_mismo-tema"
    assert b.name == a.name + "-2"
    assert not (a / ".work").exists()


def test_output_dir_setting(tmp_data, fakes, prof, tmp_path_factory):
    out = tmp_path_factory.mktemp("custom")
    d = pipeline.run_job("tema", prof, {"output_dir": str(out)}, SCRIPT, threading.Event(), Rec())
    assert d.parent == out


def test_failure_cleans_everything(tmp_data, fakes, prof, monkeypatch):
    def boom(*a, **k):
        raise UserError("Falta la clave de Pexels", "Ajustes")
    monkeypatch.setattr(pipeline, "find_media", boom)
    with pytest.raises(UserError):
        pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec())
    assert list((tmp_data / "salidas").iterdir()) == []


def test_cancel_cleans_everything(tmp_data, fakes, prof):
    cancel = threading.Event()

    def rec(key, idx, total, pct):
        if key == "voice":
            cancel.set()
    with pytest.raises(JobCancelled):
        pipeline.run_job("tema", prof, {}, SCRIPT, cancel, rec)
    assert list((tmp_data / "salidas").iterdir()) == []


def test_job_start_prunes_the_clip_cache(tmp_data, fakes, prof):
    # I9: the cache is capped (settings cache_max_gb); least-recently-used files go at job start
    d = tmp_data / "cache"
    d.mkdir()
    old = d / "pexels_video_1_1080x1920.mp4"
    old.write_bytes(b"x" * 64)
    os.utime(old, (1_000, 1_000))
    pipeline.run_job("tema", prof, {"cache_max_gb": 1e-9}, SCRIPT, threading.Event(), Rec())
    assert not old.exists()


def test_hook_word_count_is_scene_1_first_sentence():
    stale = dataclasses.replace(SCRIPT, hook="Un gancho viejo que ya no se dice")
    assert pipeline._hook_word_count(stale) == 3  # "Leer te cambia."


def test_on_output_is_told_the_folder_right_away(tmp_data, fakes, prof):
    seen = []
    d = pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec(),
                         on_output=lambda f: seen.append((f, (f / ".work").is_dir())))
    assert seen == [(d, True)]


@pytest.mark.parametrize("kind", ["file_in_the_way", "bad_parent"])
def test_unusable_output_dir_is_a_user_error(tmp_data, fakes, prof, tmp_path, kind):
    # I7: a typo / removed drive / read-only folder gives "cause -> fix", not "Error inesperado"
    blocker = tmp_path / "no-es-carpeta.txt"
    blocker.write_text("x")
    target = blocker if kind == "file_in_the_way" else blocker / "videos"
    with pytest.raises(UserError) as e:
        pipeline.run_job("tema", prof, {"output_dir": str(target)}, SCRIPT, threading.Event(), Rec())
    assert e.value.cause == f"No se puede usar la carpeta de salida {target}"
    assert e.value.fix == "Elige otra carpeta en Ajustes"


def test_failed_download_gets_one_replacement_clip(tmp_data, fakes, prof, monkeypatch, media_dir):
    # M2: one dead clip URL no longer fails the whole job
    asked = []
    real_find = pipeline.find_media

    def find(keywords, need, *rest):
        asked.append(need)
        return real_find(keywords, need, *rest)

    def download(m):
        if m.id == "m1":
            raise UserError("No se pudo descargar un clip de Pexels", "x")
        return media_dir / "v_port.mp4"

    monkeypatch.setattr(pipeline, "find_media", find)
    monkeypatch.setattr(pipeline.cache, "download", download)
    d = pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec())
    assert (d / "video.mp4").is_file()
    assert 1 in asked[1:]  # a replacement for the dead clip was asked for


def test_replacement_download_failing_too_fails_the_job(tmp_data, fakes, prof, monkeypatch):
    def download(m):
        raise UserError("No se pudo descargar un clip de Pexels", "Inténtalo de nuevo")

    monkeypatch.setattr(pipeline.cache, "download", download)
    with pytest.raises(UserError, match="descargar"):
        pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec())
    assert list((tmp_data / "salidas").iterdir()) == []


def test_render_failure_is_a_user_error_and_logged(tmp_data, fakes, prof, monkeypatch, caplog):
    # M4: e.g. a full disk; the original stays in the log
    def boom(*a, **k):
        raise RuntimeError("ffmpeg failed: No space left on device")

    monkeypatch.setattr(pipeline, "render_video", boom)
    with caplog.at_level(logging.ERROR, logger="baitd.pipeline"):
        with pytest.raises(UserError) as e:
            pipeline.run_job("tema", prof, {}, SCRIPT, threading.Event(), Rec())
    assert str(e.value) == "No se pudo renderizar el video → Comprueba el espacio en disco y vuelve a intentarlo"
    assert "No space left on device" in caplog.text
