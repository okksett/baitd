from types import SimpleNamespace

import pytest

from baitd import config
from baitd.live import channels, store
from baitd.live.buffer import Cut
from baitd.live.channels import LiveStatus
from baitd.live.session import LiveManager
from baitd.web import server

from test_live_session import FakeCapture, wait_for
from test_server import LocalClient, queue, runner  # noqa: F401  (fixtures)


@pytest.fixture
def live(tmp_data):
    FakeCapture.made = []
    m = LiveManager(tmp_data, config.load_settings, capture_factory=FakeCapture, tick_s=0.05,
                    status_fn=lambda ch: LiveStatus(True, title="En vivo", viewers=12, name="Westcol"))
    yield m
    m.close()


@pytest.fixture
def client(queue, live):
    a = server.create_app(queue, live=live)
    a.test_client_class = LocalClient
    return a.test_client()


def _add(client, url="https://kick.com/westcol", mine=False):
    r = client.post("/api/live/channels", json={"url": url, "mine": mine})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_channel_crud(client):
    ch = _add(client)
    assert ch["platform"] == "kick" and ch["handle"] == "westcol" and ch["mine"] is False
    r = client.get("/api/live/channels")
    assert r.status_code == 200
    assert [c["id"] for c in r.get_json()["channels"]] == [ch["id"]]
    assert wait_for(lambda: client.get("/api/live/channels").get_json()["channels"][0]["live"] is True)
    row = client.get("/api/live/channels").get_json()["channels"][0]
    assert row["title"] == "En vivo" and row["viewers"] == 12 and row["name"] == "Westcol"

    r = client.patch(f"/api/live/channels/{ch['id']}", json={"mine": True, "framing": "partido", "delay_s": 10})
    assert r.status_code == 200
    saved = channels.load_channels()[0]
    assert (saved.mine, saved.framing, saved.delay_s) == (True, "partido", 10.0)
    assert client.patch(f"/api/live/channels/{ch['id']}", json={"delay_s": None}).status_code == 200
    assert channels.load_channels()[0].delay_s is None
    for bad in ({"framing": "x"}, {"mine": "si"}, {"delay_s": 99}, {"delay_s": True}):
        assert client.patch(f"/api/live/channels/{ch['id']}", json=bad).status_code == 400
    assert client.patch(f"/api/live/channels/{'f' * 12}", json={"mine": True}).status_code == 404

    assert client.post("/api/live/channels", json={"url": "https://kick.com/westcol", "mine": False}).status_code == 400
    assert client.post("/api/live/channels", json={"url": "https://example.com/x", "mine": False}).status_code == 400
    assert client.post("/api/live/channels", json={"url": 3, "mine": False}).status_code == 400

    assert client.post("/api/live/channels/refresh", json={}).status_code == 200
    r = client.delete(f"/api/live/channels/{ch['id']}", json={})
    assert r.status_code == 200 and channels.load_channels() == []
    assert client.delete(f"/api/live/channels/{ch['id']}", json={}).status_code == 404


def test_mutating_routes_need_json(client):
    assert client.post("/api/live/mark", data="x", content_type="text/plain").status_code == 415
    assert client.delete("/api/live/capture").status_code == 415


def test_capture_and_409_on_deleting_the_captured_channel(client, live):
    ch = _add(client)
    r = client.post("/api/live/capture", json={"channel_id": ch["id"]})
    assert r.status_code == 200 and r.get_json()["state"] == "capturing"
    r = client.post("/api/live/capture", json={"channel_id": ch["id"]})
    assert r.status_code == 400 and "Ya hay una captura en marcha" in r.get_json()["error"]
    r = client.delete(f"/api/live/channels/{ch['id']}", json={})
    assert r.status_code == 409 and "→" in r.get_json()["error"]
    st = client.get("/api/live/status?since=0").get_json()
    assert st["state"] == "capturing" and st["channel_id"] == ch["id"]
    assert client.delete("/api/live/capture", json={}).status_code == 200
    assert wait_for(lambda: client.get("/api/live/status").get_json()["state"] == "idle")
    assert client.delete(f"/api/live/channels/{ch['id']}", json={}).status_code == 200


def test_capture_unknown_channel(client):
    r = client.post("/api/live/capture", json={"channel_id": "c" * 12})
    assert r.status_code == 400 and r.get_json()["error"].startswith("Ese canal ya no está en la lista")


def test_mark_without_capture(client):
    r = client.post("/api/live/mark", json={})
    assert r.status_code == 200 and r.get_json()["kind"] == "no_capture"
    st = client.get("/api/live/status?since=0").get_json()
    assert st["state"] == "idle" and st["events"][-1]["kind"] == "no_capture"
    assert client.get(f"/api/live/status?since={st['events'][-1]['id']}").get_json()["events"] == []
    assert client.get("/api/live/status?since=x").status_code == 200


def test_moments_and_thumb(client, make_mp4):
    cut = Cut(make_mp4(seconds=12, w=320, h=180, fps=15), 3.0, 9.0, 12.0, False)
    ch = SimpleNamespace(platform="kick", handle="westcol", mine=False, framing="cara", url="https://kick.com/westcol")
    aid = store.save_moment(cut, channel=ch, title="Gran jugada", reason="manual", at=1_700_000_000.0,
                            session_id="s1", framing="cara")
    r = client.get("/api/live/moments")
    assert r.status_code == 200
    assert [m["analysis_id"] for m in r.get_json()["moments"]] == [aid]
    r = client.get(f"/api/live/moments/{aid}/thumb")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.data[:2] == b"\xff\xd8"
    assert client.get("/api/live/moments/zzz/thumb").status_code == 404
    assert client.get(f"/api/live/moments/{'0' * 12}/thumb").status_code == 404


@pytest.mark.parametrize("key, value, ok", [
    ("live_before_s", 500, False), ("live_before_s", 9, False), ("live_before_s", 30, True),
    ("live_before_s", "30", True), ("live_before_s", True, False), ("live_before_s", 30.5, False),
    ("live_after_s", 4, False), ("live_after_s", 60, True), ("live_ai_per_hour", 31, False),
    ("live_ai_per_hour", 0, True), ("live_max_height", 1440, False), ("live_max_height", 720, True),
    ("live_hotkey", "", False), ("live_hotkey", "x" * 41, False), ("live_hotkey", "Ctrl+F9", True)])
def test_live_settings_validation(client, key, value, ok):
    r = client.post("/api/settings", json={key: value})
    assert (r.status_code == 200) is ok, r.get_json()
    if ok:
        assert config.load_settings()[key] == (int(value) if key != "live_hotkey" else value)
    else:
        assert "→" in r.get_json()["error"]


def test_live_settings_defaults():
    d = config.SETTINGS_DEFAULTS
    assert (d["live_hotkey"], d["live_before_s"], d["live_after_s"], d["live_ai_per_hour"], d["live_max_height"]) \
        == ("F9", 45, 15, 6, 1080)


def test_app_builds_its_own_manager_without_threads(queue):
    import threading
    before = {t.name for t in threading.enumerate()}
    a = server.create_app(queue)
    a.test_client_class = LocalClient
    assert a.test_client().post("/api/live/mark", json={}).get_json()["kind"] == "no_capture"
    assert {t.name for t in threading.enumerate()} - before == set()
