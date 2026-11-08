import json
import shutil

import pytest

from baitd import config
from baitd.clip import pipeline, sources
from baitd.clip.models import ClipProfile
from baitd.web import server

from test_server import BASE, LocalClient, app, queue, runner  # noqa: F401  (fixtures)

UP = {"Content-Type": "application/octet-stream", "X-Baitd-Upload": "1"}


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def profile_id(client):
    return client.get("/api/profiles").get_json()["profiles"][0]["id"]


def _upload(client, path, name="clip.mp4", headers=UP):
    return client.post(f"/api/uploads?name={name}", data=path.read_bytes(), headers=headers)


def _source(make_mp4, seconds=60):
    sid, path = sources.new_upload("a.mp4")
    shutil.copy(make_mp4(seconds=seconds, w=320, h=180, fps=15), path)
    return sources.finish_upload(sid, path, "a.mp4")


def _analysis(src, aid="abcdef012345", moments=None):
    moments = moments if moments is not None else [
        {"start": 5.0, "end": 25.0, "kind": "gracioso", "score": 80, "title": "T", "description": "d",
         "hashtags": ["x"], "text": "hola", "framing": "cara", "credit": ""}]
    data = {"source_id": src.id, "profile": ClipProfile().to_dict(), "created_at": "2026-01-01T00:00:00",
            "transcript": {"language": "es", "words": [], "duration": src.duration}, "moments": moments}
    (pipeline.analyses_dir() / f"{aid}.json").write_text(json.dumps(data), encoding="utf-8")
    return aid


def test_upload_requires_octet_stream_and_header(client, make_mp4):
    data = make_mp4(seconds=2, w=320, h=180, fps=15).read_bytes()
    r = client.post("/api/uploads?name=a.mp4", data={"f": (__import__("io").BytesIO(data), "a.mp4")})
    assert r.status_code == 415 and "→" in r.get_json()["error"]
    assert client.post("/api/uploads?name=a.mp4", json={"a": 1}).status_code == 415
    assert client.post("/api/uploads?name=a.mp4", data=data,
                       headers={"Content-Type": "application/octet-stream"}).status_code == 415
    assert client.post("/api/uploads?name=a.mp4", data=data, headers={**UP, "X-Baitd-Upload": "0"}).status_code == 415
    # the exemption is only for the upload route
    assert client.post("/api/settings", data=b"x", headers=UP).status_code == 415
    assert not any(sources.sources_dir().iterdir())


def test_upload_rejects_foreign_host(client, make_mp4):
    r = client.post("/api/uploads?name=a.mp4", data=b"x", headers=UP, base_url="http://evil.example:5000")
    assert r.status_code == 403
    assert not any(sources.sources_dir().iterdir())


def test_upload_streams_and_probes(client, make_mp4):
    path = make_mp4(seconds=3, w=320, h=180, fps=15)
    r = _upload(client, path, name="C:\\mis videos\\../Mi video 日本.mp4")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert set(body) == {"source_id", "title", "duration", "has_video"}
    assert body["title"] == "Mi video 日本" and body["has_video"] is True and 2.5 < body["duration"] < 3.5
    src = sources.load_source(body["source_id"])
    assert src.path.read_bytes() == path.read_bytes() and src.path.parent.parent == sources.sources_dir()


def test_upload_failures_leave_nothing(client, make_mp4):
    assert _upload(client, make_mp4(seconds=2, w=320, h=180, fps=15), name="a.exe").status_code == 400
    r = client.post("/api/uploads?name=a.mp4", data=b"not a video", headers=UP)
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert not any(sources.sources_dir().iterdir())


def test_upload_no_space(client, monkeypatch, make_mp4):
    monkeypatch.setattr(sources, "free_bytes", lambda p: 10)
    r = _upload(client, make_mp4(seconds=2, w=320, h=180, fps=15))
    assert r.status_code == 400 and "espacio" in r.get_json()["error"]
    assert not any(sources.sources_dir().iterdir())


def test_upload_missing_content_length_and_too_big(client, make_mp4):
    big = server._UPLOAD_MAX + 1
    r = client.post("/api/uploads?name=a.mp4", data=b"", headers=UP, environ_overrides={"CONTENT_LENGTH": str(big)})
    assert r.status_code == 400 and "grande" in r.get_json()["error"]
    r = client.post("/api/uploads?name=a.mp4", headers=UP, environ_overrides={"wsgi.input_terminated": True},
                    input_stream=__import__("io").BytesIO(b"abc"))
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert not any(sources.sources_dir().iterdir())


def test_clips_auto_and_review_create_jobs(client, profile_id, queue, make_mp4):
    r = client.post("/api/clips", json={"profile_id": profile_id, "mode": "auto",
                                        "url": "https://youtube.com/watch?v=abc", "overrides": {"count": 3}})
    assert r.status_code == 200
    src = _source(make_mp4)
    r2 = client.post("/api/clips", json={"profile_id": profile_id, "mode": "review", "source_id": src.id})
    assert r2.status_code == 200
    items = {i["id"]: i for i in queue._items}
    a, b = items[r.get_json()["job_id"]], items[r2.get_json()["job_id"]]
    assert a["kind"] == "clip_auto" and a["payload"] == {"source": {"url": "https://youtube.com/watch?v=abc"},
                                                         "overrides": {"count": 3}}
    assert b["kind"] == "clip_analyze" and b["payload"]["source"] == {"source_id": src.id}


def test_clips_rejects_blocked_url_and_bad_source_args(client, profile_id, make_mp4):
    def post(**kw):
        return client.post("/api/clips", json={"profile_id": profile_id, "mode": "auto", **kw})

    r = post(url="https://www.tiktok.com/@a/video/1")
    assert r.status_code == 400 and "TikTok" in r.get_json()["error"]
    assert post().status_code == 400
    src = _source(make_mp4)
    assert post(url="https://youtube.com/watch?v=a", source_id=src.id).status_code == 400
    assert post(source_id="0123456789ab").status_code == 400  # unknown source
    assert post(source_id="../../x").status_code == 400
    assert post(url="https://youtube.com/watch?v=a", overrides={"count": 99}).status_code == 400
    assert post(url="https://youtube.com/watch?v=a", overrides="x").status_code == 400
    assert client.post("/api/clips", json={"profile_id": profile_id, "mode": "x", "url": "https://a.b/c"}).status_code == 400
    assert client.post("/api/clips", json={"profile_id": "nope", "mode": "auto", "url": "https://a.b/c"}).status_code == 400


def test_analysis_get_export_validation_delete(client, profile_id, queue, tmp_data, make_mp4):
    src = _source(make_mp4, seconds=60)
    aid = _analysis(src)
    r = client.get(f"/api/analyses/{aid}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["language"] == "es" and body["moments"][0]["title"] == "T"
    assert set(body["source"]) == {"id", "title", "channel", "url", "duration", "width", "height", "fps", "has_video"}
    assert client.get("/api/analyses/ffffffffffff").status_code == 404
    assert client.get("/api/analyses/..%2Fx").status_code == 404

    ok = {"start": 5, "end": 25, "framing": "centro", "title": "Nuevo", "credit": "yo"}
    r = client.post(f"/api/analyses/{aid}/export", json={"moments": [ok], "profile_id": profile_id})
    assert r.status_code == 200
    job = next(i for i in queue._items if i["id"] == r.get_json()["job_id"])
    assert job["kind"] == "clip_export" and job["analysis_id"] == aid
    m = job["payload"]["moments"][0]
    assert (m["title"], m["framing"], m["credit"], m["hashtags"]) == ("Nuevo", "centro", "yo", ["x"])

    expected = "Un clip tiene inicio o fin no válido → Ajusta el inicio y el fin del clip"
    for bad in ({**ok, "end": 10},            # 5 s < 10 s
                {**ok, "end": 300},           # > 180 s and beyond the source
                {**ok, "start": 55, "end": 75},  # past the 60 s source
                {**ok, "start": -1},
                {**ok, "start": "5"},
                {**ok, "end": float("nan")},
                {**ok, "start": True},
                "x"):
        r = client.post(f"/api/analyses/{aid}/export", json={"moments": [bad]})
        assert r.status_code == 400 and r.get_json()["error"] == expected, bad
    assert client.post(f"/api/analyses/{aid}/export", json={"moments": []}).status_code == 400
    assert client.post(f"/api/analyses/{aid}/export", json={"moments": [{**ok, "framing": "x"}]}).status_code == 400
    assert client.post("/api/analyses/ffffffffffff/export", json={"moments": [ok]}).status_code == 404

    # delete: unfinished export item references the analysis -> the source stays
    r = client.delete(f"/api/analyses/{aid}", json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert not (pipeline.analyses_dir() / f"{aid}.json").exists()
    assert client.delete(f"/api/analyses/{aid}", json={}).status_code == 404


def test_delete_analysis_removes_unreferenced_source_unless_kept(client, queue, make_mp4):
    queue.stop(timeout=2)
    src = _source(make_mp4)
    aid = _analysis(src)
    assert client.delete(f"/api/analyses/{aid}", json={}).status_code == 200
    with pytest.raises(Exception):
        sources.load_source(src.id)

    src2 = _source(make_mp4)
    aid2 = _analysis(src2, aid="abcdef012346")
    config.save_settings({"keep_sources": True})
    assert client.delete(f"/api/analyses/{aid2}", json={}).status_code == 200
    assert sources.load_source(src2.id).id == src2.id


def test_delete_analysis_keeps_source_needed_by_queued_job(client, profile_id, queue, make_mp4):
    src = _source(make_mp4)
    aid = _analysis(src)
    client.post("/api/clips", json={"profile_id": profile_id, "mode": "review", "source_id": src.id})
    assert client.delete(f"/api/analyses/{aid}", json={}).status_code == 200
    assert sources.load_source(src.id).id == src.id


def test_source_media_supports_range(client, make_mp4):
    src = _source(make_mp4)
    r = client.get(f"/api/sources/{src.id}/media", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.data) == 100
    assert client.get("/api/sources/0123456789ab/media").status_code == 404
    assert client.get("/api/sources/..%2F..%2Fx/media").status_code == 404


def test_source_media_rejects_cross_site(client, make_mp4):
    src = _source(make_mp4)
    r = client.get(f"/api/sources/{src.id}/media", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert client.get(f"/api/sources/{src.id}/media", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_ytdlp_update_route(client, monkeypatch):
    monkeypatch.setattr(server.ytdlp_loader, "update_ytdlp", lambda cancel=None: "2026.01.01")
    assert client.post("/api/ytdlp/update", json={}).get_json() == {"version": "2026.01.01"}
    assert client.post("/api/ytdlp/update", data=b"x", headers=UP).status_code == 415


def test_settings_keep_sources(client):
    assert client.post("/api/settings", json={"keep_sources": True}).get_json()["keep_sources"] is True
    assert client.post("/api/settings", json={"keep_sources": "yes"}).status_code == 400
    assert config.load_settings()["keep_sources"] is True


def test_delete_unused_upload(client, profile_id, queue, make_mp4):
    src = _source(make_mp4, seconds=2)
    assert client.delete(f"/api/sources/{src.id}", data=b"{}", headers=UP).status_code == 415  # JSON only
    r = client.delete(f"/api/sources/{src.id}", json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert not (sources.sources_dir() / src.id).exists()
    assert client.delete(f"/api/sources/{src.id}", json={}).status_code == 404
    for bad in ("ABCDEF012345", "0123456789a", "..%2Fx"):
        assert client.delete(f"/api/sources/{bad}", json={}).status_code == 404


def test_delete_source_in_use_is_409(client, profile_id, queue, make_mp4):
    used = _source(make_mp4, seconds=2)
    client.post("/api/clips", json={"profile_id": profile_id, "mode": "review", "source_id": used.id})  # pending job
    analysed = _source(make_mp4, seconds=2)
    _analysis(analysed)
    for src in (used, analysed):
        r = client.delete(f"/api/sources/{src.id}", json={})
        assert r.status_code == 409 and "→" in r.get_json()["error"]
        assert sources.load_source(src.id).id == src.id
