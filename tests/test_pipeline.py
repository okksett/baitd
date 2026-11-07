import dataclasses
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
    assert (d / "guion.txt").read_text(encoding="utf-8").split("\n\n")[:2] == ["Leer cambia tu vida", "Leer te cambia."]
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
