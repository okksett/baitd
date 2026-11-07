import json
import logging
import threading
import time

import pytest

from baitd import config
from baitd.jobqueue import JobQueue
from baitd.models import JobCancelled, Scene, Script, UserError

SCRIPT = Script(title="T", hook="H.", scenes=[Scene("Uno dos.", ["a", "b"])],
                description="d", hashtags=["x"], language="es")
WAIT = 3.0


@pytest.fixture(autouse=True)
def _data(tmp_data):
    return tmp_data


def wait_for(cond, timeout=WAIT):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def statuses(q):
    return [i["status"] for i in q.list()]


def make_queue(tmp_path, runner):
    return JobQueue(tmp_path / "queue.json", runner=runner)


def test_script_round_trip():
    assert Script.from_dict(SCRIPT.to_dict()) == SCRIPT
    assert Script.from_dict(json.loads(json.dumps(SCRIPT.to_dict()))) == SCRIPT


def test_runs_sequentially(tmp_path):
    lock = threading.Lock()
    state = {"now": 0, "max": 0, "order": []}

    def runner(topic, profile, settings, script, cancel, progress, **kw):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            state["order"].append(topic)
        time.sleep(0.01)
        with lock:
            state["now"] -= 1
        return tmp_path / topic

    q = make_queue(tmp_path, runner)
    q.add(["a", "b", "c"], config.default_profile())
    q.start()
    assert wait_for(lambda: statuses(q) == ["done"] * 3)
    q.stop()
    assert state["max"] == 1
    assert state["order"] == ["a", "b", "c"]
    assert q.list()[0]["output"] == str(tmp_path / "a")


def test_batch_continues_after_failure(tmp_path):
    def runner(topic, profile, settings, script, cancel, progress, **kw):
        if topic == "b":
            raise UserError("Falta la clave de Pexels", "Ajustes")
        return tmp_path / topic

    q = make_queue(tmp_path, runner)
    q.add(["a", "b", "c"], config.default_profile())
    q.start()
    assert wait_for(lambda: "pending" not in statuses(q) and "running" not in statuses(q))
    q.stop()
    assert statuses(q) == ["done", "error", "done"]
    assert q.list()[1]["error"] == "Falta la clave de Pexels → Ajustes"


def test_unexpected_error_is_sanitised_and_logged(tmp_path, caplog):
    def runner(*a, **k):
        raise RuntimeError("secret /path/trace")

    q = make_queue(tmp_path, runner)
    q.add(["a"], config.default_profile())
    with caplog.at_level(logging.ERROR):
        q.start()
        assert wait_for(lambda: statuses(q) == ["error"])
    q.stop()
    err = q.list()[0]["error"]
    assert err == "Error inesperado: RuntimeError → Revisa el registro y vuelve a intentarlo"
    assert "secret" not in err
    assert "secret /path/trace" in caplog.text and "Traceback" in caplog.text


def test_cancel_pending_and_running(tmp_path):
    started = threading.Event()

    def runner(topic, profile, settings, script, cancel, progress, **kw):
        started.set()
        cancel.wait(WAIT)
        raise JobCancelled()

    q = make_queue(tmp_path, runner)
    ids = q.add(["a", "b"], config.default_profile())
    q.cancel(ids[1])  # pending -> cancelled immediately, before any worker exists
    assert statuses(q) == ["pending", "cancelled"]
    q.start()
    assert started.wait(WAIT)
    assert statuses(q)[0] == "running"
    q.cancel(ids[0])
    assert wait_for(lambda: statuses(q) == ["cancelled", "cancelled"])
    q.cancel(ids[0])  # no-op on finished
    assert statuses(q) == ["cancelled", "cancelled"]
    q.stop()


def test_queue_restores_running_as_pending(tmp_path):
    path = tmp_path / "queue.json"
    q = JobQueue(path, runner=lambda *a, **k: tmp_path)
    ids = q.add(["a", "b"], config.default_profile())
    data = json.loads(path.read_text(encoding="utf-8"))
    data[0].update(status="running", step="voice", step_index=3, percent=40.0)
    path.write_text(json.dumps(data), encoding="utf-8")

    q2 = JobQueue(path, runner=lambda *a, **k: tmp_path)
    assert [i["id"] for i in q2.list()] == ids
    assert statuses(q2) == ["pending", "pending"]
    assert q2.list()[0]["percent"] == 0.0
    assert json.loads(path.read_text(encoding="utf-8"))[0]["status"] == "pending"


def test_corrupt_queue_is_quarantined(tmp_path):
    path = tmp_path / "queue.json"
    path.write_text("{not json", encoding="utf-8")
    q = JobQueue(path, runner=lambda *a, **k: tmp_path)
    assert q.list() == []
    assert list(tmp_path.glob("queue.json.corrupt-*"))
    q.add(["a"], config.default_profile())
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 1


def test_snapshot_script_settings_and_progress(tmp_path):
    seen = {}
    at_step = threading.Event()
    proceed = threading.Event()

    def runner(topic, profile, settings, script, cancel, progress, **kw):
        seen.update(profile=profile, settings=settings, script=script)
        progress("voice", 3, 6, 50)
        at_step.set()
        proceed.wait(WAIT)
        return tmp_path / "out"

    profile = config.default_profile()
    q = make_queue(tmp_path, runner)
    q.add(["a"], profile, scripts={"a": SCRIPT})
    profile.name = "Editado"  # later edits must not leak into the queued job
    profile.voice = "otra"
    config.save_settings({"pexels_key": "k-123"})
    q.start()
    assert at_step.wait(WAIT)
    item = q.list()[0]
    assert (item["status"], item["step"], item["step_index"], item["percent"]) == ("running", "voice", 3, 41.7)
    assert item["profile_name"] == "Por defecto" and item["created_at"]
    proceed.set()
    assert wait_for(lambda: statuses(q) == ["done"])
    q.stop()
    assert seen["profile"].name == "Por defecto" and seen["profile"].voice == "es-ES-ElviraNeural"
    assert seen["script"] == SCRIPT
    assert seen["settings"]["pexels_key"] == "k-123"
    assert q.list()[0]["percent"] == 100.0


def test_stop_returns_running_job_to_pending(tmp_path):
    started = threading.Event()

    def runner(topic, profile, settings, script, cancel, progress, **kw):
        started.set()
        cancel.wait(WAIT)
        raise JobCancelled()

    path = tmp_path / "queue.json"
    q = JobQueue(path, runner=runner)
    q.add(["a", "b"], config.default_profile())
    q.start()
    assert started.wait(WAIT)
    q.stop()
    assert statuses(q) == ["pending", "pending"]
    assert [i["status"] for i in json.loads(path.read_text(encoding="utf-8"))] == ["pending", "pending"]


def test_remove_and_clear_finished(tmp_path):
    def runner(topic, *a, **k):
        if topic == "b":
            raise UserError("x", "y")
        return tmp_path

    q = make_queue(tmp_path, runner)
    ids = q.add(["a", "b", "c"], config.default_profile())
    q.cancel(ids[2])
    q.remove(ids[0])  # pending: not removable
    assert len(q.list()) == 3
    q.start()
    assert wait_for(lambda: statuses(q)[:2] == ["done", "error"])
    q.stop()
    q.remove(ids[0])
    assert [i["id"] for i in q.list()] == ids[1:]
    q.clear_finished()
    assert q.list() == []
    assert json.loads((tmp_path / "queue.json").read_text(encoding="utf-8")) == []


def test_running_job_records_its_output_folder(tmp_path):
    # I5: the folder is on the item (and in queue.json) as soon as the pipeline creates it
    recorded = threading.Event()
    proceed = threading.Event()
    folder = tmp_path / "salida-a"

    def runner(topic, profile, settings, script, cancel, progress, on_output=None):
        folder.mkdir()
        on_output(folder)
        recorded.set()
        proceed.wait(WAIT)
        return folder

    path = tmp_path / "queue.json"
    q = JobQueue(path, runner=runner)
    q.add(["a"], config.default_profile())
    q.start()
    assert recorded.wait(WAIT)
    assert q.list()[0]["output"] == str(folder)
    assert json.loads(path.read_text(encoding="utf-8"))[0]["output"] == str(folder)
    proceed.set()
    assert wait_for(lambda: statuses(q) == ["done"])
    q.stop()


def test_failed_job_does_not_keep_the_removed_folder(tmp_path):
    def runner(topic, profile, settings, script, cancel, progress, on_output=None):
        on_output(tmp_path / "gone")
        raise UserError("x", "y")

    q = make_queue(tmp_path, runner)
    q.add(["a"], config.default_profile())
    q.start()
    assert wait_for(lambda: statuses(q) == ["error"])
    q.stop()
    assert q.list()[0]["output"] == ""


def test_load_removes_partial_folder_of_a_killed_job(tmp_path):
    # I5: the app died mid-render (console closed): the half-made folder is removed on restart
    path = tmp_path / "queue.json"
    q = JobQueue(path, runner=lambda *a, **k: tmp_path)
    q.add(["a", "b"], config.default_profile())
    partial = tmp_path / "salidas" / "2026-11-07_a"
    (partial / ".work").mkdir(parents=True)
    (partial / ".work" / "shot_000.mp4").write_bytes(b"x")
    (partial / "video.mp4").write_bytes(b"half")
    finished = tmp_path / "salidas" / "2026-11-07_b"  # no .work: a finished video, never deleted
    finished.mkdir()
    (finished / "video.mp4").write_bytes(b"ok")
    data = json.loads(path.read_text(encoding="utf-8"))
    data[0].update(status="running", output=str(partial))
    data[1].update(status="running", output=str(finished))
    path.write_text(json.dumps(data), encoding="utf-8")

    q2 = JobQueue(path, runner=lambda *a, **k: tmp_path)
    assert not partial.exists()
    assert (finished / "video.mp4").exists()
    assert statuses(q2) == ["pending", "pending"]
    assert [i["output"] for i in q2.list()] == ["", ""]
    assert [i["output"] for i in json.loads(path.read_text(encoding="utf-8"))] == ["", ""]
