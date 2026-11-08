import json
import shutil
import threading
import time

import pytest

from baitd import config, selftest
from baitd.clip import framing, pipeline, sources
from baitd.clip.models import Transcript
from baitd.ffmpeg import probe
from baitd.jobqueue import JobQueue
from baitd.models import JobCancelled, UserError, Word

WAIT = 60.0
SECONDS = 40


def _words():
    """80 words, 2 per second; every 10th ends a sentence: sentence k spans [5k, 5k + 4.9]."""
    return [Word(f"palabra{i}." if i % 10 == 9 else f"palabra{i}", i * 0.5, i * 0.5 + 0.4) for i in range(80)]


def _llm(prompt):
    def one(start, end, score, title):
        return {"start": start, "end": end, "kind": "interesante", "score": score, "title": title,
                "description": f"Descripcion {title}", "hashtags": ["prueba", "#clip"]}
    return json.dumps({"moments": [one("00:00", "00:10", 90, "Primero"), one("00:20", "00:30", 80, "Segundo")]})


@pytest.fixture
def providers(make_mp4):
    """Everything network- or model-facing replaced: a generated 40 s source, a fake transcript and LLM."""
    seen = {"fetch": 0, "faces": 0}
    src_file = make_mp4(seconds=SECONDS, w=320, h=180, fps=15)

    def fetch_url(url, cancel, on_progress):
        seen["fetch"] += 1
        sid, target = sources.new_upload("origen.mp4")
        shutil.copy(src_file, target)
        on_progress(0.5)
        info = sources.finish_upload(sid, target, "Mi video ñ.mp4")
        info.channel = "Canal"
        return info

    def transcribe(audio, provider, language, settings, model, cancel, on_progress, on_model_load=None):
        assert audio.is_file()
        if seen.get("model_load"):  # a local model that has to be downloaded first
            on_model_load(True)
            on_model_load(False)
        on_progress(1.0)
        return Transcript("es", _words(), float(SECONDS))

    def detect_faces(src, start, end, fps, cancel):
        seen["faces"] += 1
        return []

    p = pipeline.ClipProviders(fetch_url=fetch_url, transcribe=transcribe, complete=_llm, detect_faces=detect_faces)
    p.seen = seen
    return p


def _item(kind="clip_auto", payload=None, **overrides):
    profile = config.default_profile()
    payload = payload if payload is not None else {
        "source": {"url": "https://example.test/watch?v=1"},
        "overrides": {"count": 2, "min_s": 10, "max_s": 20, **overrides}}
    return {"id": "j" * 12, "topic": "x", "kind": kind, "payload": payload, "profile": profile.to_dict()}


def _settings(tmp_path, **extra):
    return {"output_dir": str(tmp_path / "out"), **extra}


def test_auto_end_to_end_offline(tmp_data, tmp_path, providers):
    calls, outputs, titles = [], [], []
    folder = pipeline.run_clip_auto(_item(), _settings(tmp_path), threading.Event(),
                                    lambda *a: calls.append(a), providers,
                                    on_output=outputs.append, on_title=titles.append)
    assert outputs == [folder] and titles == ["Mi video ñ"]
    assert folder.parent == tmp_path / "out" and folder.name.endswith("_mi-video-n")
    assert sorted(p.name for p in folder.iterdir()) == ["clip-01.mp4", "clip-02.mp4", "publicacion.txt"]  # no .work
    for name in ("clip-01.mp4", "clip-02.mp4"):
        info = probe(folder / name)
        assert (info["width"], info["height"]) == (1080, 1920)
        assert abs(info["fps"] - 30) < 0.01
        assert 10 <= info["duration"] <= 20
    text = (folder / "publicacion.txt").read_text(encoding="utf-8")
    assert "clip-01.mp4 (00:00:00–00:00:14)" in text and "clip-02.mp4 (00:00:20–00:00:34)" in text
    assert "Primero" in text and "Descripcion Segundo" in text and "#prueba #clip" in text
    assert text.index("clip-01") < text.index("clip-02")
    assert {c[0] for c in calls} == set(pipeline.CLIP_STEPS)
    assert all(c[2] == 5 and c[1] == pipeline.CLIP_STEPS.index(c[0]) + 1 for c in calls)
    assert providers.seen["faces"] == 2
    assert not list(sources.sources_dir().iterdir())  # the source goes after a successful run


def test_model_download_shows_its_own_step_text(tmp_data, tmp_path, providers):
    providers.seen["model_load"] = True
    calls = []
    pipeline.run_clip_analyze(_item("clip_analyze"), _settings(tmp_path), threading.Event(),
                              lambda *a: calls.append(a), providers)
    labels = [c[0] for c in calls if c[1] == 2]
    assert labels[:3] == ["transcribe", pipeline.MODEL_STEP, "transcribe"]  # back to "transcribing" after the load
    assert all(c[1:3] == (2, 5) for c in calls if c[0] == pipeline.MODEL_STEP)
    assert pipeline.MODEL_STEP in pipeline.CLIP_STEP_LABELS and pipeline.MODEL_STEP not in pipeline.CLIP_STEPS


def test_analyze_then_export_with_edited_bounds(tmp_data, tmp_path, providers):
    settings = _settings(tmp_path)
    runners = {
        "clip_analyze": lambda *a, **k: pipeline.run_clip_analyze(*a, providers=providers, **k),
        "clip_export": lambda *a, **k: pipeline.run_clip_export(*a, providers=providers, **k),
    }
    qpath = tmp_path / "queue.json"
    q = JobQueue(qpath, clip_runners=runners)
    config.save_settings(settings)
    q.add_clip_job("clip_analyze", _item("clip_analyze")["payload"], config.default_profile())
    q.start()
    deadline = time.monotonic() + WAIT
    while q.list()[0]["status"] != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    q.stop()
    done = q.list()[0]
    assert done["status"] == "done" and done["output"] == "" and len(done["analysis_id"]) == 12
    assert done["topic"] == "Mi video ñ"  # on_title replaced the link
    analysis = pipeline.load_analysis(done["analysis_id"])
    assert [m["title"] for m in analysis["moments"]] == ["Primero", "Segundo"]
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())  # analyze writes no clips

    q2 = JobQueue(qpath, clip_runners=runners)  # the analysis survives a reload
    assert q2.list()[0]["analysis_id"] == done["analysis_id"]
    moments = analysis["moments"][:1]
    moments[0]["start"] = 2.0  # edited bounds: 2.0 .. 14.9
    moments[0]["title"] = "Editado"
    jid = q2.add_clip_job("clip_export", {"analysis_id": done["analysis_id"], "moments": moments},
                          config.default_profile())
    assert q2.list()[1]["analysis_id"] == done["analysis_id"] and q2.list()[1]["kind"] == "clip_export"
    q2.start()
    deadline = time.monotonic() + WAIT
    while q2.list()[1]["status"] != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    q2.stop()
    job = next(i for i in q2.list() if i["id"] == jid)
    assert job["status"] == "done", job["error"]
    from pathlib import Path
    folder = Path(job["output"])
    assert [p.name for p in sorted(folder.iterdir())] == ["clip-01.mp4", "publicacion.txt"]
    assert abs(probe(folder / "clip-01.mp4")["duration"] - 12.9) < 0.4
    text = (folder / "publicacion.txt").read_text(encoding="utf-8")
    assert "(00:00:02–00:00:14)" in text and "Editado" in text
    with pytest.raises(UserError):  # the source and its analysis are gone after the export
        pipeline.load_analysis(done["analysis_id"])
    assert not list(sources.sources_dir().iterdir())


def test_export_missing_source_message(tmp_data, tmp_path, providers):
    path = pipeline.run_clip_analyze(_item("clip_analyze"), _settings(tmp_path), threading.Event(),
                                     lambda *a: None, providers)
    analysis = pipeline.load_analysis(path.stem)
    sources.delete_source(analysis["source_id"])
    gone = "El video original ya no está → Vuelve a pegar el enlace o subir el archivo"
    item = _item("clip_export", {"analysis_id": path.stem, "moments": analysis["moments"]})
    with pytest.raises(UserError) as e:
        pipeline.run_clip_export(item, _settings(tmp_path), threading.Event(), lambda *a: None, providers)
    assert str(e.value) == gone
    assert not path.exists()  # an analysis whose source is gone is deleted
    item = _item("clip_export", {"analysis_id": "0" * 12, "moments": analysis["moments"]})
    with pytest.raises(UserError) as e:  # no such analysis
        pipeline.run_clip_export(item, _settings(tmp_path), threading.Event(), lambda *a: None, providers)
    assert str(e.value) == gone
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_analysis_ids_and_malformed_files(tmp_data, providers):
    for bad in ("../x", "A" * 12, "abc", "", None, "0" * 13):
        with pytest.raises(UserError):
            pipeline.load_analysis(bad)
        pipeline.delete_analysis(bad)  # no-op
    good = {"source_id": "a" * 12, "profile": {}, "created_at": "now",
            "transcript": {"language": "es", "duration": 5.0, "words": [{"text": "a", "start": 0.0, "end": 0.5}]},
            "moments": [{"start": 1.0, "end": 4.0, "kind": "gracioso", "score": 50, "title": "t",
                         "description": "", "hashtags": [], "text": ""}]}
    damaged = "El análisis está dañado → Vuelve a buscar momentos"
    cases = [("not json", None),
             ("start after end", {**good, "moments": [{**good["moments"][0], "start": 5.0, "end": 4.0}]}),
             ("nan", {**good, "moments": [{**good["moments"][0], "end": float("nan")}]}),
             ("hashtags type", {**good, "moments": [{**good["moments"][0], "hashtags": "x"}]}),
             ("bad word", {**good, "transcript": {**good["transcript"], "words": [{"text": 1}]}}),
             ("bad profile", {**good, "profile": {"count": 99}}),
             ("not an object", [1, 2])]
    for i, (_name, data) in enumerate(cases):
        f = pipeline.analyses_dir() / f"{i:012x}.json"
        f.write_text("{oops" if data is None else json.dumps(data), encoding="utf-8")
        with pytest.raises(UserError) as e:
            pipeline.load_analysis(f.stem)
        assert str(e.value) == damaged
    ok = pipeline.analyses_dir() / ("f" * 12 + ".json")
    ok.write_text(json.dumps(good), encoding="utf-8")
    assert pipeline.load_analysis("f" * 12)["source_id"] == "a" * 12
    pipeline.delete_analysis("f" * 12)
    assert not ok.exists()


def test_export_rejects_bad_payload_moments(tmp_data, tmp_path, providers):
    path = pipeline.run_clip_analyze(_item("clip_analyze"), _settings(tmp_path), threading.Event(),
                                     lambda *a: None, providers)
    m = pipeline.load_analysis(path.stem)["moments"][0]
    for bad in ([], [{**m, "start": 9.0, "end": 8.0}], [{**m, "end": 5000.0}], [{**m, "start": "x"}], ["nope"],
                [{**m, "start": 5.0, "end": 5.01}], [{**m, "start": 0.0, "end": 9.9}],
                [{**m, "start": 0.0, "end": 180.5}]):
        item = _item("clip_export", {"analysis_id": path.stem, "moments": bad})
        with pytest.raises(UserError):
            pipeline.run_clip_export(item, _settings(tmp_path), threading.Event(), lambda *a: None, providers)
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())
    assert pipeline.analyses_dir().joinpath(path.name).exists()  # a rejected export keeps the analysis


def test_cancel_removes_folder_and_partial_source(tmp_data, tmp_path, providers):
    cancel = threading.Event()

    def complete(prompt):
        cancel.set()  # the user cancels while the AI is working: source and output folder already exist
        return _llm(prompt)

    providers.complete = complete
    outputs = []
    with pytest.raises(JobCancelled):
        pipeline.run_clip_auto(_item(), _settings(tmp_path), cancel, lambda *a: None, providers,
                               on_output=outputs.append)
    assert outputs and not outputs[0].exists()
    assert not list((tmp_path / "out").iterdir())
    assert not list(sources.sources_dir().iterdir())


def test_failure_removes_folder_but_keeps_an_uploaded_source(tmp_data, tmp_path, providers, make_mp4):
    sid, target = sources.new_upload("a.mp4")
    shutil.copy(make_mp4(seconds=SECONDS, w=320, h=180, fps=15), target)
    sources.finish_upload(sid, target, "a.mp4")
    providers.complete = lambda prompt: "no json at all"
    item = _item(payload={"source": {"source_id": sid}, "overrides": {"count": 2, "min_s": 10, "max_s": 20}})
    with pytest.raises(UserError) as e:
        pipeline.run_clip_auto(item, _settings(tmp_path), threading.Event(), lambda *a: None, providers)
    assert "habla" in str(e.value)
    assert not list((tmp_path / "out").iterdir())
    assert sources.load_source(sid).id == sid


@pytest.mark.parametrize("keep", [False, True])
def test_source_deleted_after_export_unless_keep(tmp_data, tmp_path, providers, keep):
    settings = _settings(tmp_path, keep_sources=keep)
    path = pipeline.run_clip_analyze(_item("clip_analyze", count=1), settings, threading.Event(),
                                     lambda *a: None, providers)
    analysis = pipeline.load_analysis(path.stem)
    item = _item("clip_export", {"analysis_id": path.stem, "moments": analysis["moments"][:1]})
    pipeline.run_clip_export(item, settings, threading.Event(), lambda *a: None, providers)
    assert [p.name for p in sources.sources_dir().iterdir()] == ([analysis["source_id"]] if keep else [])
    assert path.exists() is keep


def test_two_exports_of_one_analysis_release_source_after_the_last(tmp_data, tmp_path, providers):
    config.save_settings(_settings(tmp_path))
    path = pipeline.run_clip_analyze(_item("clip_analyze", count=1), _settings(tmp_path), threading.Event(),
                                     lambda *a: None, providers)
    analysis = pipeline.load_analysis(path.stem)
    runners = {"clip_export": lambda *a, **k: pipeline.run_clip_export(*a, providers=providers, **k)}
    q = JobQueue(tmp_path / "queue.json", clip_runners=runners)
    payload = {"analysis_id": path.stem, "moments": analysis["moments"][:1]}
    q.add_clip_job("clip_export", payload, config.default_profile())
    q.add_clip_job("clip_export", payload, config.default_profile())
    states = []

    def after_first():
        rows = q.list()
        if rows[0]["status"] == "done" and not states:
            states.append((path.exists(), len(list(sources.sources_dir().iterdir()))))
        return rows[1]["status"] in ("done", "error")

    q.start()
    assert _wait(after_first, WAIT)
    q.stop()
    assert [i["status"] for i in q.list()] == ["done", "done"], [i["error"] for i in q.list()]
    assert states and states[0][1] == 1  # the source was still there when the first export finished
    assert not path.exists() and not list(sources.sources_dir().iterdir())


def test_bad_overrides_are_the_clipping_error(tmp_data, tmp_path, providers):
    item = _item(count=99)
    with pytest.raises(UserError) as e:
        pipeline.run_clip_auto(item, _settings(tmp_path), threading.Event(), lambda *a: None, providers)
    assert str(e.value) == "Ajustes de clipping no válidos → Revisa número de clips y duración en el perfil"
    assert providers.seen["fetch"] == 0


# ---- queue ----

def _write_queue(path, items):
    path.write_text(json.dumps(items), encoding="utf-8")


def _old_item(**kw):
    return {"id": "a" * 12, "topic": "tema", "status": "pending", "step": "", "step_index": 0, "percent": 0.0,
            "error": "", "output": "", "profile_name": "P", "created_at": "t",
            "profile": config.default_profile().to_dict(), "script": None, **kw}


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_queue_old_items_default_to_short(tmp_path):
    path = tmp_path / "queue.json"
    _write_queue(path, [_old_item()])
    ran = []

    def runner(topic, profile, settings, script, cancel, progress, **kw):
        ran.append(topic)
        return tmp_path / "salida"

    q = JobQueue(path, runner=runner)
    assert q.list()[0]["kind"] == "short" and q.list()[0]["analysis_id"] == ""
    q.start()
    assert _wait(lambda: q.list()[0]["status"] == "done")
    q.stop()
    assert ran == ["tema"]


def test_queue_clip_kinds_dispatch_and_persist(tmp_path):
    path = tmp_path / "queue.json"
    seen = []
    out = tmp_path / "carpeta"

    def auto(item, settings, cancel, progress, **kw):
        seen.append(("auto", item["payload"], item["profile"]["name"]))
        kw["on_title"]("Titulo del video")
        out.mkdir()
        kw["on_output"](out)
        progress("render", 5, 5, 50.0)
        return out

    def analyze(item, settings, cancel, progress, **kw):
        seen.append(("analyze", item["payload"], item["topic"]))
        return tmp_path / "analisis" / ("ab" * 6 + ".json")

    def export(item, settings, cancel, progress, **kw):
        raise UserError("El video original ya no está", "Vuelve")

    q = JobQueue(path, clip_runners={"clip_auto": auto, "clip_analyze": analyze, "clip_export": export})
    profile = config.default_profile()
    a = q.add_clip_job("clip_auto", {"source": {"url": "https://x.test/v"}, "overrides": {"count": 3}}, profile)
    b = q.add_clip_job("clip_analyze", {"source": {"source_id": "c" * 12}, "overrides": {}}, profile)
    c = q.add_clip_job("clip_export", {"analysis_id": "d" * 12, "moments": []}, profile)
    profile.name = "Editado"  # later edits do not leak into queued jobs
    with pytest.raises(ValueError):
        q.add_clip_job("clip_nope", {}, profile)
    rows = {i["id"]: i for i in q.list()}
    assert [rows[x]["kind"] for x in (a, b, c)] == ["clip_auto", "clip_analyze", "clip_export"]
    assert rows[a]["topic"] == "https://x.test/v" and rows[c]["analysis_id"] == "d" * 12
    q2 = JobQueue(path, clip_runners=q._clip_runners)  # persisted
    assert [i["kind"] for i in q2.list()] == ["clip_auto", "clip_analyze", "clip_export"]
    q2.start()
    assert _wait(lambda: [i["status"] for i in q2.list()] == ["done", "done", "error"])
    q2.stop()
    rows = {i["id"]: i for i in q2.list()}
    assert seen[0] == ("auto", {"source": {"url": "https://x.test/v"}, "overrides": {"count": 3}}, "Por defecto")
    assert rows[a]["topic"] == "Titulo del video" and rows[a]["output"] == str(out)
    assert rows[b]["analysis_id"] == "ab" * 6 and rows[b]["output"] == ""
    assert rows[c]["error"] == "El video original ya no está → Vuelve"
    assert json.loads(path.read_text(encoding="utf-8"))[1]["analysis_id"] == "ab" * 6


def test_running_clip_item_partial_folder_cleaned_on_load(tmp_path):
    path = tmp_path / "queue.json"
    partial = tmp_path / "salidas" / "2026-11-07_video"
    (partial / ".work" / "clip-01").mkdir(parents=True)
    (partial / "clip-01.mp4").write_bytes(b"half")
    payload = {"source": {"url": "https://x.test/v"}, "overrides": {}}
    _write_queue(path, [_old_item(kind="clip_auto", payload=payload, status="running", output=str(partial),
                                  step="render", step_index=5, percent=70.0)])
    q = JobQueue(path)
    assert not partial.exists()
    row = q.list()[0]
    assert (row["status"], row["output"], row["step_index"], row["kind"]) == ("pending", "", 0, "clip_auto")
    # a clip item that lost its payload cannot run: it is dropped instead of crashing the worker
    _write_queue(path, [_old_item(kind="clip_auto")])
    assert JobQueue(path).list() == []


def test_selftest_includes_clips(tmp_data, tmp_path_factory):
    work = tmp_path_factory.mktemp("selftest-clips")
    selftest.run_selftest(work)
    folder = next(p for p in (work / "out").iterdir() if (p / "clip-01.mp4").exists())
    assert sorted(p.name for p in folder.iterdir()) == ["clip-01.mp4", "clip-02.mp4", "publicacion.txt"]
    for name in ("clip-01.mp4", "clip-02.mp4"):
        info = probe(folder / name)
        assert (info["width"], info["height"]) == (1080, 1920)
        assert abs(info["fps"] - 30) < 0.01


# ---- lifecycle: startup sweep and releasing what a removed queue item held ----

def _clip_row(id, kind, status, payload, analysis_id=""):
    return _old_item(id=id, kind=kind, status=status, payload=payload, analysis_id=analysis_id)


def _fake_source(sid):
    d = sources.sources_dir() / sid
    d.mkdir(parents=True)
    (d / "source.mp4").write_bytes(b"x")
    (d / "meta.json").write_text("{}", encoding="utf-8")  # a finished source; a partial upload has none
    return d


def _fake_analysis(aid, sid):
    f = pipeline.analyses_dir() / f"{aid}.json"
    f.write_text(json.dumps({"source_id": sid}), encoding="utf-8")
    return f


def _sweep_fixture(tmp_data):
    s = {k: _fake_source(k * 12) for k in "abcdef"}  # a: listed analysis, b: pending job, c: pending export's
    partial = sources.sources_dir() / ("9" * 12)  # an upload cut by a crash: no meta.json
    partial.mkdir()
    (partial / "source.mp4").write_bytes(b"half")
    (partial / "upload.json").write_text('{"name": "a.mp4", "size": 10, "file": "source.mp4"}', encoding="utf-8")
    an = {"listed": _fake_analysis("1" * 12, "a" * 12), "export": _fake_analysis("2" * 12, "c" * 12),
          "orphan": _fake_analysis("3" * 12, "d" * 12), "of_removed": _fake_analysis("4" * 12, "e" * 12)}
    path = tmp_data / "queue.json"
    _write_queue(path, [
        _clip_row("q1" + "0" * 10, "clip_analyze", "done", {"source": {"source_id": "a" * 12}}, "1" * 12),
        _clip_row("q2" + "0" * 10, "clip_auto", "pending", {"source": {"source_id": "b" * 12}}),
        _clip_row("q3" + "0" * 10, "clip_export", "pending", {"analysis_id": "2" * 12, "moments": []}, "2" * 12),
        _clip_row("q4" + "0" * 10, "clip_auto", "error", {"source": {"source_id": "f" * 12}}),
    ])
    return JobQueue(path), s, an, partial


def test_startup_sweep_removes_what_nothing_needs(tmp_data):
    q, s, an, partial = _sweep_fixture(tmp_data)
    q.sweep_clip_data(keep_sources=False)
    assert an["listed"].exists() and an["export"].exists()
    assert not an["orphan"].exists() and not an["of_removed"].exists()
    assert sorted(p.name for p in sources.sources_dir().iterdir()) == ["a" * 12, "b" * 12, "c" * 12]
    assert not partial.exists()


def test_startup_sweep_keeps_sources_when_asked(tmp_data):
    q, s, an, partial = _sweep_fixture(tmp_data)
    q.sweep_clip_data(keep_sources=True)
    assert not an["orphan"].exists()  # analyses no item lists cannot be reached: they go anyway
    assert all(d.exists() for d in s.values())
    assert not partial.exists()  # an upload never finished before this start is not a source: it goes anyway


def _analyze_done_queue(tmp_data, *extra):
    _fake_source("a" * 12)
    aid = "1" * 12
    f = _fake_analysis(aid, "a" * 12)
    path = tmp_data / "queue.json"
    rows = [_clip_row("q1" + "0" * 10, "clip_analyze", "done", {"source": {"url": "https://x.test/v"}}, aid), *extra]
    _write_queue(path, rows)
    return JobQueue(path), f


def test_removing_a_done_analysis_releases_it_and_its_source(tmp_data):
    q, f = _analyze_done_queue(tmp_data)
    q.remove("q1" + "0" * 10)
    assert q.list() == [] and not f.exists()
    assert not list(sources.sources_dir().iterdir())


def test_clearing_finished_releases_analysis_but_honours_keep_sources(tmp_data):
    config.save_settings({"keep_sources": True})
    q, f = _analyze_done_queue(tmp_data)
    q.clear_finished()
    assert not f.exists()
    assert [p.name for p in sources.sources_dir().iterdir()] == ["a" * 12]


def test_removing_a_done_analysis_keeps_what_an_unfinished_export_needs(tmp_data):
    export = _clip_row("q2" + "0" * 10, "clip_export", "pending", {"analysis_id": "1" * 12, "moments": []}, "1" * 12)
    q, f = _analyze_done_queue(tmp_data, export)
    q.remove("q1" + "0" * 10)
    assert f.exists() and [p.name for p in sources.sources_dir().iterdir()] == ["a" * 12]


def test_removing_a_failed_run_releases_its_unused_upload(tmp_data):
    _fake_source("b" * 12)
    _fake_source("c" * 12)
    _fake_analysis("5" * 12, "c" * 12)  # another analysis still needs source c
    path = tmp_data / "queue.json"
    _write_queue(path, [_clip_row("q1" + "0" * 10, "clip_auto", "error", {"source": {"source_id": "b" * 12}}),
                        _clip_row("q2" + "0" * 10, "clip_auto", "cancelled", {"source": {"source_id": "c" * 12}})])
    q = JobQueue(path)
    q.clear_finished()
    assert [p.name for p in sources.sources_dir().iterdir()] == ["c" * 12]


def test_export_uses_the_export_time_title_credit_and_music(tmp_data, tmp_path, providers, monkeypatch):
    path = pipeline.run_clip_analyze(_item("clip_analyze", count=1), _settings(tmp_path, keep_sources=True),
                                     threading.Event(), lambda *a: None, providers)
    analysis = pipeline.load_analysis(path.stem)
    assert (analysis["profile"]["hook_title"], analysis["profile"]["credit"], analysis["profile"]["music"]) == \
        (True, True, False)
    seen = []

    def fake_render(src, m, words, plan, profile, ropts, workdir, out, cancel, on_progress):
        seen.append((profile.clipping, m.credit))
        out.write_bytes(b"x")

    monkeypatch.setattr(pipeline, "render_clip", fake_render)
    profile = config.default_profile()
    profile.clipping.hook_title, profile.clipping.credit, profile.clipping.music = False, False, True
    profile.clipping.count = 7  # anything else comes from the analysis
    moments = [analysis["moments"][0], {**analysis["moments"][0], "credit": "Yo"}]
    item = {**_item("clip_export", {"analysis_id": path.stem, "moments": moments}), "profile": profile.to_dict()}
    pipeline.run_clip_export(item, _settings(tmp_path, keep_sources=True), threading.Event(), lambda *a: None,
                             providers)
    clip, credit = seen[0]
    assert (clip.hook_title, clip.credit, clip.music) == (False, False, True)
    assert clip.count == analysis["profile"]["count"] and clip.min_s == analysis["profile"]["min_s"]
    assert seen[1][1] == "Yo"


def test_framing_failure_falls_back_to_centre(tmp_data, tmp_path, providers, monkeypatch, caplog):
    real = framing.plan_crop
    modes = []

    def flaky(src, start, end, w, h, mode, cancel, faces=None, cuts=None):
        modes.append(mode)
        if mode == "cara":
            raise RuntimeError("scene detection exploded")
        return real(src, start, end, w, h, mode, cancel, faces=faces, cuts=cuts)

    monkeypatch.setattr(framing, "plan_crop", flaky)
    with caplog.at_level("WARNING"):
        folder = pipeline.run_clip_auto(_item(), _settings(tmp_path), threading.Event(), lambda *a: None, providers)
    assert modes.count("centro") == 2 and (folder / "clip-01.mp4").is_file()
    assert "se usa el centro" in caplog.text
