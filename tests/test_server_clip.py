import io
import json
import shutil
import threading
from pathlib import Path

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


class _Untouchable(io.RawIOBase):
    """A request body that records whether anything read it (the guard must answer before that)."""
    def __init__(self):
        self.read_called = False

    def readable(self):
        return True

    def readinto(self, b):
        self.read_called = True
        return 0

    def seekable(self):  # the test client only asks where the stream starts
        return True

    def seek(self, pos, whence=0):
        return 0


def _begin(client, name="clip.mp4", size=10):
    return client.post("/api/uploads", json={"name": name, "size": size})


def _put(client, uid, offset, data, headers=UP):
    return client.put(f"/api/uploads/{uid}?offset={offset}", data=data, headers=headers)


def _upload(client, path, name="clip.mp4", chunk=None):
    """The browser's protocol: begin (JSON), PUT the bytes in order, then finish (JSON)."""
    data = path.read_bytes()
    r = _begin(client, name, len(data))
    assert r.status_code == 200, r.get_json()
    uid = r.get_json()["upload_id"]
    chunk = chunk or len(data)
    for off in range(0, len(data), chunk):
        r = _put(client, uid, off, data[off:off + chunk])
        assert r.status_code == 200 and r.get_json() == {"offset": min(len(data), off + chunk)}, r.get_json()
    return client.post(f"/api/uploads/{uid}/finish", json={})


def _stored(uid):
    return next(p for p in (sources.sources_dir() / uid).iterdir() if p.name.startswith("source."))


def test_upload_guard_rejects_before_reading_the_body(client):
    uid = _begin(client).get_json()["upload_id"]
    for method, url in (("POST", "/api/uploads"), ("PUT", f"/api/uploads/{uid}?offset=0")):
        body = _Untouchable()  # a cross-site "simple" request: text/plain, no custom header
        r = client.open(url, method=method, input_stream=body, headers={"Content-Type": "text/plain"},
                        environ_overrides={"CONTENT_LENGTH": "5"})
        assert r.status_code == 415 and "→" in r.get_json()["error"]
        assert not body.read_called
    assert _stored(uid).stat().st_size == 0


def test_upload_exemption_is_exactly_the_chunk_put(client):
    uid = _begin(client).get_json()["upload_id"]
    refused = [
        client.post("/api/uploads", data=b"x", headers=UP),  # begin is JSON now
        client.post(f"/api/uploads/{uid}/finish", data=b"x", headers=UP),
        client.delete(f"/api/uploads/{uid}", data=b"x", headers=UP),
        client.put(f"/api/uploads/{uid}/finish", data=b"x", headers=UP),
        client.put(f"/api/uploads/{uid.upper()}?offset=0", data=b"x", headers=UP),
        client.put(f"/api/uploads/{uid}0?offset=0", data=b"x", headers=UP),
        client.put("/api/settings", data=b"x", headers=UP),
        client.post("/api/settings", data=b"x", headers=UP),
        _put(client, uid, 0, b"x", headers={"Content-Type": "application/octet-stream"}),
        _put(client, uid, 0, b"x", headers={**UP, "X-Baitd-Upload": "0"}),
        _put(client, uid, 0, b"x", headers={"Content-Type": "text/plain", "X-Baitd-Upload": "1"}),
        client.put(f"/api/uploads/{uid}?offset=0", json={"a": 1}),
        client.put(f"/api/uploads/{uid}?offset=0", data={"f": (io.BytesIO(b"x"), "a.mp4")}),
    ]
    assert [r.status_code for r in refused] == [415] * len(refused)
    assert _stored(uid).stat().st_size == 0
    assert _put(client, uid, 0, b"x").status_code == 200


def test_upload_rejects_foreign_host(client):
    evil = "http://evil.example:5000"
    assert client.post("/api/uploads", json={"name": "a.mp4", "size": 1}, base_url=evil).status_code == 403
    assert not any(sources.sources_dir().iterdir())
    uid = _begin(client).get_json()["upload_id"]
    assert client.put(f"/api/uploads/{uid}?offset=0", data=b"x", headers=UP, base_url=evil).status_code == 403
    assert _stored(uid).stat().st_size == 0


def test_upload_in_chunks_probes_and_registers(client, make_mp4):
    path = make_mp4(seconds=3, w=320, h=180, fps=15)
    r = _upload(client, path, name="C:\\mis videos\\../Mi video 日本.mp4", chunk=path.stat().st_size // 3 + 1)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert set(body) == {"source_id", "title", "duration", "has_video"}
    assert body["title"] == "Mi video 日本" and body["has_video"] is True and 2.5 < body["duration"] < 3.5
    src = sources.load_source(body["source_id"])
    assert src.path.read_bytes() == path.read_bytes() and src.path.parent.parent == sources.sources_dir()
    assert sorted(p.name for p in src.path.parent.iterdir()) == ["meta.json", "source.mp4"]  # no upload state left
    # a registered source is no longer an upload: no more chunks, no second finish, no abort through this route
    uid = body["source_id"]
    assert _put(client, uid, src.path.stat().st_size, b"x").status_code == 404
    assert client.post(f"/api/uploads/{uid}/finish", json={}).status_code == 404
    assert client.delete(f"/api/uploads/{uid}", json={}).status_code == 404
    assert sources.load_source(uid).path.read_bytes() == path.read_bytes()


def test_upload_offset_must_match_what_was_received(client):
    uid = _begin(client, size=10).get_json()["upload_id"]
    assert _put(client, uid, 0, b"12345").get_json() == {"offset": 5}
    for off in (0, 3, 7):
        r = _put(client, uid, off, b"abc")
        assert r.status_code == 409 and r.get_json()["offset"] == 5 and "→" in r.get_json()["error"]
    for off in ("", "x", "-1", "5.0"):
        r = client.put(f"/api/uploads/{uid}?offset={off}", data=b"abc", headers=UP)
        assert r.status_code in (400, 409) and "→" in r.get_json()["error"]
    r = client.post(f"/api/uploads/{uid}/finish", json={})  # not all bytes yet
    assert r.status_code == 409 and r.get_json()["offset"] == 5
    r = _put(client, uid, 5, b"678901")  # more than the declared size
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert _stored(uid).read_bytes() == b"12345"
    assert _put(client, uid, 5, b"67890").get_json() == {"offset": 10}


def test_upload_rejected_before_any_byte_is_stored(client, monkeypatch):
    bad = [{"name": "a.exe", "size": 10}, {"name": "video", "size": 10}, {"name": "", "size": 10},
           {"name": 5, "size": 10}, {"name": "a" * 300 + ".mp4", "size": 10}, {"size": 10},
           {"name": "a.mp4", "size": 0}, {"name": "a.mp4", "size": -1}, {"name": "a.mp4", "size": True},
           {"name": "a.mp4", "size": "10"}, {"name": "a.mp4", "size": 1.5}, {"name": "a.mp4"},
           {"name": "a.mp4", "size": server._UPLOAD_MAX + 1}]
    for body in bad:
        r = client.post("/api/uploads", json=body)
        assert r.status_code == 400 and "→" in r.get_json()["error"], body
    assert "grande" in client.post("/api/uploads", json=bad[-1]).get_json()["error"]
    monkeypatch.setattr(sources, "free_bytes", lambda p: 10)
    r = _begin(client, size=1000)
    assert r.status_code == 400 and "espacio" in r.get_json()["error"]
    assert not any(sources.sources_dir().iterdir())


def test_upload_chunk_limits(client):
    uid = _begin(client, size=server.UPLOAD_CHUNK_MAX * 2).get_json()["upload_id"]
    r = client.put(f"/api/uploads/{uid}?offset=0", data=b"", headers=UP,
                   environ_overrides={"CONTENT_LENGTH": str(server.UPLOAD_CHUNK_MAX + 1)})
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    r = client.put(f"/api/uploads/{uid}?offset=0", headers={**UP, "Transfer-Encoding": "chunked"},
                   input_stream=io.BytesIO(b"abc"))  # no Content-Length
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert _put(client, uid, 0, b"").status_code == 400  # an empty chunk
    r = client.put(f"/api/uploads/{uid}?offset=0", headers=UP, input_stream=io.BytesIO(b"ab"),
                   environ_overrides={"CONTENT_LENGTH": "5"})  # the connection drops mid-chunk
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert _stored(uid).stat().st_size == 0  # a cut chunk is not kept
    assert _put(client, "0" * 12, 0, b"x").status_code == 404  # unknown upload


def test_upload_finish_failure_leaves_nothing(client):
    uid = _begin(client, size=11).get_json()["upload_id"]
    _put(client, uid, 0, b"not a video")
    r = client.post(f"/api/uploads/{uid}/finish", json={})
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert not any(sources.sources_dir().iterdir())


def test_upload_abort_removes_the_partial_file(client):
    uid = _begin(client, size=10).get_json()["upload_id"]
    _put(client, uid, 0, b"12345")
    assert client.delete(f"/api/uploads/{uid}", data=b"{}", headers=UP).status_code == 415  # JSON only
    r = client.delete(f"/api/uploads/{uid}", json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert not any(sources.sources_dir().iterdir())
    assert _put(client, uid, 5, b"67890").status_code == 404
    assert client.delete(f"/api/uploads/{uid}", json={}).status_code == 404
    for bad in ("ABCDEF012345", "0123456789a", "..%2Fx"):
        assert client.delete(f"/api/uploads/{bad}", json={}).status_code == 404


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

    # delete: refused while an unfinished export needs the analysis
    r = client.delete(f"/api/analyses/{aid}", json={})
    assert r.status_code == 409 and "→" in r.get_json()["error"]
    assert (pipeline.analyses_dir() / f"{aid}.json").exists()
    for i in list(queue._items):
        queue.cancel(i["id"])
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


def test_settings_youtube_cookies_browser(client):
    assert client.get("/api/settings").get_json()["yt_cookies_browser"] == ""
    assert client.post("/api/settings", json={"yt_cookies_browser": "firefox"}).get_json()["yt_cookies_browser"] == "firefox"
    r = client.post("/api/settings", json={"yt_cookies_browser": "netscape"})
    assert r.status_code == 400 and "navegador" in r.get_json()["error"]
    assert config.load_settings()["yt_cookies_browser"] == "firefox"
    assert client.post("/api/settings", json={"yt_cookies_browser": ""}).get_json()["yt_cookies_browser"] == ""


def test_settings_youtube_cookies_file(client, tmp_path):
    good = tmp_path / "cookies.txt"
    good.write_text("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-value\n", encoding="utf-8")
    body = client.post("/api/settings", json={"yt_cookies_file": f'"{good}"'}).get_json()
    assert body["yt_cookies_file"] == str(good)  # quotes from "Copy as path" are dropped
    assert "secret-value" not in json.dumps(body)  # the path is stored, never the cookies
    bad = tmp_path / "notas.txt"
    bad.write_text("no son cookies", encoding="utf-8")
    for value in (str(bad), str(tmp_path / "missing.txt")):
        assert client.post("/api/settings", json={"yt_cookies_file": value}).status_code == 400
    assert config.load_settings()["yt_cookies_file"] == str(good)
    assert client.post("/api/settings", json={"yt_cookies_file": ""}).get_json()["yt_cookies_file"] == ""


def test_delete_unused_upload(client, profile_id, queue, make_mp4):
    src = _source(make_mp4, seconds=2)
    assert client.delete(f"/api/sources/{src.id}", data=b"{}", headers=UP).status_code == 415  # JSON only
    r = client.delete(f"/api/sources/{src.id}", json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert not (sources.sources_dir() / src.id).exists()
    assert client.delete(f"/api/sources/{src.id}", json={}).status_code == 404
    for bad in ("ABCDEF012345", "0123456789a", "..%2Fx"):
        assert client.delete(f"/api/sources/{bad}", json={}).status_code == 404


def test_delete_source_refuses_an_unfinished_upload(client):
    """A folder without meta.json is an upload in progress: only DELETE /api/uploads/<id> may remove it."""
    folder = sources.sources_dir() / ("7" * 12)
    folder.mkdir(parents=True)
    (folder / "source.mp4").write_bytes(b"half")
    assert client.delete(f"/api/sources/{'7' * 12}", json={}).status_code == 404
    assert folder.exists()


def test_delete_source_in_use_is_409(client, profile_id, queue, make_mp4):
    used = _source(make_mp4, seconds=2)
    client.post("/api/clips", json={"profile_id": profile_id, "mode": "review", "source_id": used.id})  # pending job
    analysed = _source(make_mp4, seconds=2)
    _analysis(analysed)
    for src in (used, analysed):
        r = client.delete(f"/api/sources/{src.id}", json={})
        assert r.status_code == 409 and "→" in r.get_json()["error"]
        assert sources.load_source(src.id).id == src.id


def test_export_with_edited_bounds_keeps_the_moment_texts(client, profile_id, queue, tmp_data, make_mp4):
    src = _source(make_mp4, seconds=30)
    other = {"start": 1.0, "end": 12.0, "kind": "historia", "score": 60, "title": "Otro", "description": "otra",
             "hashtags": ["y"], "text": "", "framing": "cara", "credit": ""}
    first = {"start": 5.0, "end": 25.0, "kind": "gracioso", "score": 80, "title": "T", "description": "Descripción",
             "hashtags": ["risa", "#clip"], "text": "hola", "framing": "cara", "credit": ""}
    aid = _analysis(src, moments=[other, first])
    edited = {"start": 6.0, "end": 16.5, "framing": "centro", "title": "Nuevo", "credit": "", "orig": 1}
    r = client.post(f"/api/analyses/{aid}/export", json={"moments": [edited], "profile_id": profile_id})
    assert r.status_code == 200, r.get_json()
    item = next(i for i in queue._items if i["id"] == r.get_json()["job_id"])
    m = item["payload"]["moments"][0]
    assert (m["kind"], m["score"], m["description"], m["hashtags"], m["text"]) == \
        ("gracioso", 80, "Descripción", ["risa", "#clip"], "hola")
    assert (m["start"], m["end"], m["title"], m["framing"]) == (6.0, 16.5, "Nuevo", "centro")
    assert "orig" not in m

    # a bad index falls back to the exact-bounds match (or to nothing)
    for orig in (5, -1, True, "1", None):
        r = client.post(f"/api/analyses/{aid}/export", json={"moments": [{**edited, "orig": orig}]})
        m = next(i for i in queue._items if i["id"] == r.get_json()["job_id"])["payload"]["moments"][0]
        assert m.get("description", "") == "" and "orig" not in m, orig
    r = client.post(f"/api/analyses/{aid}/export", json={"moments": [{**edited, "start": 1.0, "end": 12.0, "orig": 9}]})
    m = next(i for i in queue._items if i["id"] == r.get_json()["job_id"])["payload"]["moments"][0]
    assert m["description"] == "otra"

    # the clip job renders with those texts
    folder = pipeline.run_clip_export(dict(item), {"output_dir": str(tmp_data / "out")}, threading.Event(),
                                      lambda *a: None)
    text = (folder / "publicacion.txt").read_text(encoding="utf-8")
    assert "Nuevo" in text and "Descripción" in text and "#risa #clip" in text


def test_review_ui_strings():
    """String checks on the shipped UI (no browser): headings, candidate text, keep note, switch guard."""
    from baitd.web import server
    web = Path(server.__file__).resolve().parent
    html = (web / "templates" / "index.html").read_text(encoding="utf-8")
    js = (web / "static" / "app.js").read_text(encoding="utf-8")
    css = (web / "static" / "app.css").read_text(encoding="utf-8")
    assert "<h4" not in html and '<h3 class="script-topic">' in html
    assert ".review .script-topic" in css and "-webkit-line-clamp: 3" in css
    assert 'class="m-text"' in html and "text.textContent = m.text" in js and "innerHTML" not in js
    assert 'id="clip-keep-note"' in html and "clip-keep-note" in js
    assert "reviewEdited(open)" in js and 'tr("clip.switch_confirm")' in js


def test_page_boot_data_carries_the_upload_extensions(client):
    html = client.get("/").get_data(as_text=True)
    boot = json.loads(html.split('id="boot">', 1)[1].split("</script>", 1)[0])
    assert boot["upload_exts"] == list(sources.UPLOAD_EXTS)  # the browser checks the extension before it starts
