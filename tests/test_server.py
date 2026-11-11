import json
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from markupsafe import escape

from baitd import config
from baitd.jobqueue import JobQueue
from baitd.models import UserError
from baitd.web import server

WEB = Path(server.__file__).resolve().parent
ES = WEB / "static" / "i18n" / "es.json"
EN = WEB / "static" / "i18n" / "en.json"
INDEX = WEB / "templates" / "index.html"
CSS = WEB / "static" / "app.css"
JS = WEB / "static" / "app.js"
BASE = "http://127.0.0.1:5000"
SCRIPT = {"title": "T", "hook": "Hola.", "scenes": [{"text": "Hola. Mundo.", "keywords": ["a", "b"]}],
          "description": "d", "hashtags": ["x"], "language": "es"}


class LocalClient(FlaskClient):
    """Talks to the app as the browser does: Host 127.0.0.1:5000 on server port 5000."""

    def open(self, *args, **kwargs):
        kwargs.setdefault("base_url", BASE)
        return super().open(*args, **kwargs)


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class FakeRunner:
    """Stands in for pipeline.run_job: writes video.mp4 into a fresh folder."""

    def __init__(self, root: Path):
        self.root = root
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, topic, profile, settings, script, cancel, progress, on_output=None):
        with self.lock:
            self.calls.append((topic, script))
            folder = self.root / f"out-{len(self.calls)}"
        progress("script", 1, 6, 100.0)
        folder.mkdir(parents=True)
        (folder / "video.mp4").write_bytes(b"\x00" * 4096)
        return folder


@pytest.fixture
def runner(tmp_data):
    return FakeRunner(tmp_data / "salidas")


@pytest.fixture
def queue(tmp_data, runner):
    q = JobQueue(tmp_data / "queue.json", runner=runner)
    yield q
    q.stop(timeout=2)


@pytest.fixture
def app(queue):
    a = server.create_app(queue)
    a.test_client_class = LocalClient
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def finished_job(client, queue, topic="tema"):
    ids = client.post("/api/jobs", json={"topics": [topic], "profile_id": "default"}).json["ids"]
    queue.start()
    assert wait_for(lambda: client.get("/api/jobs").json["jobs"][-1]["status"] == "done")
    return ids[0]


# ---- settings ----

def test_settings_masks_keys(client):
    client.post("/api/settings", json={"pexels_key": "abcdef1234"})
    r = client.get("/api/settings")
    assert r.json["pexels_key"].endswith("1234") and r.json["pexels_key"].startswith("••••")
    assert "abcdef" not in r.text


def test_masked_value_not_overwritten(client, tmp_path):
    client.post("/api/settings", json={"pexels_key": "abcdef1234"})
    masked = client.get("/api/settings").json["pexels_key"]
    videos = str(tmp_path / "videos")
    r = client.post("/api/settings", json={"pexels_key": masked, "output_dir": videos})
    assert r.status_code == 200
    assert config.load_settings()["pexels_key"] == "abcdef1234"
    assert config.load_settings()["output_dir"] == videos


def test_output_dir_is_created_and_probed(client, tmp_path):
    target = tmp_path / "nueva" / "carpeta"
    r = client.post("/api/settings", json={"output_dir": str(target)})
    assert r.status_code == 200 and target.is_dir()
    assert list(target.iterdir()) == []  # the probe file was removed
    assert client.post("/api/settings", json={"output_dir": ""}).status_code == 200  # back to default


def test_unusable_output_dir_rejected(client, tmp_path):
    # I7: same UserError as the pipeline, as a 400 in the app's {"error": "cause → fix"} format
    blocker = tmp_path / "archivo.txt"
    blocker.write_text("x")
    bad = blocker / "videos"
    r = client.post("/api/settings", json={"output_dir": str(bad), "theme": "light"})
    assert r.status_code == 400
    assert r.json["error"] == f"No se puede usar la carpeta de salida {bad} → Elige otra carpeta en Ajustes"
    assert config.load_settings()["output_dir"] == "" and config.load_settings()["theme"] == "dark"


@pytest.mark.parametrize("value, ok", [(5, True), (0.5, True), ("2", True), (0, False), (-1, False),
                                       ("x", False), (True, False)])
def test_cache_cap_setting(client, value, ok):
    r = client.post("/api/settings", json={"cache_max_gb": value})
    assert (r.status_code == 200) is ok
    if ok:
        assert config.load_settings()["cache_max_gb"] == float(value)
    else:
        assert "→" in r.json["error"] and config.load_settings()["cache_max_gb"] == 3


def test_settings_validation(client):
    r = client.post("/api/settings", json={"ui_lang": "xx"})
    assert r.status_code == 400 and "→" in r.json["error"]
    client.post("/api/settings", json={"theme": "light", "unknown": "x"})
    assert config.load_settings()["theme"] == "light"
    assert "unknown" not in config.load_settings()


# ---- profiles ----

def test_profiles_crud(client):
    profiles = client.get("/api/profiles").json["profiles"]
    assert [p["id"] for p in profiles] == ["default"]
    new = dict(profiles[0], name="Datos curiosos")
    del new["id"]
    created = client.post("/api/profiles", json=new).json
    assert created["id"] and created["id"] != "default" and created["name"] == "Datos curiosos"
    client.post("/api/profiles", json=dict(created, name="Renombrado", rate=10))
    names = {p["id"]: p["name"] for p in client.get("/api/profiles").json["profiles"]}
    assert names == {"default": "Por defecto", created["id"]: "Renombrado"}
    r = client.delete(f"/api/profiles/{created['id']}", json={})
    assert r.status_code == 200 and [p["id"] for p in r.json["profiles"]] == ["default"]


def test_delete_unknown_profile_is_404(client):
    client.get("/api/profiles")
    r = client.delete("/api/profiles/nope", json={})
    assert r.status_code == 404 and "→" in r.json["error"]


def test_concurrent_profile_saves_are_not_lost(client):
    # M9: read-modify-write of profiles.json is serialised (waitress runs several threads)
    base = client.get("/api/profiles").json["profiles"][0]

    def save(i):
        config.save_profile(server._parse_profile({**base, "id": f"p{i}", "name": f"P{i}"}))

    threads = [threading.Thread(target=save, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {p.id for p in config.list_profiles()} == {"default", *(f"p{i}" for i in range(12))}


def test_cannot_delete_last_profile(client):
    client.get("/api/profiles")
    r = client.delete("/api/profiles/default", json={})
    assert r.status_code == 400 and "→" in r.json["error"]
    assert [p.id for p in config.list_profiles()] == ["default"]


@pytest.mark.parametrize("bad", [{"name": ""}, {"rate": 99}, {"music_volume": 2}, {"fps": 24},
                                 {"music": "C:/Windows/notepad.exe"},
                                 {"subtitle": {"position": "left"}},
                                 {"subtitle": {"font": "Comic Sans"}}, {"subtitle": {"font": 3}},
                                 {"subtitle": {"outline": -1}}, {"subtitle": {"outline": 99}},
                                 {"subtitle": {"outline": "4"}}, {"subtitle": {"outline": True}},
                                 {"subtitle": {"box": "yes"}}])
def test_invalid_profile_rejected(client, bad):
    base = client.get("/api/profiles").json["profiles"][0]
    r = client.post("/api/profiles", json={**base, **bad})
    assert r.status_code == 400 and "→" in r.json["error"]


def test_valid_subtitle_overrides_accepted(client):
    base = client.get("/api/profiles").json["profiles"][0]
    sub = {**base["subtitle"], "font": "Anton", "outline": 3, "box": False}
    r = client.post("/api/profiles", json={**base, "subtitle": sub})
    assert r.status_code == 200 and r.json["subtitle"]["font"] == "Anton"


# ---- voices and music ----

def test_voices_cached_per_provider_and_lang(client, monkeypatch):
    calls = []

    def fake(provider, language, settings):
        calls.append((provider, language))
        return [{"id": f"{language}-1", "name": "Uno", "gender": "female", "free": True}]

    monkeypatch.setattr(server.tts, "list_voices", fake)
    assert client.get("/api/voices?provider=edge&lang=es").json["voices"][0]["id"] == "es-1"
    client.get("/api/voices?provider=edge&lang=es")
    client.get("/api/voices?provider=edge&lang=en")
    assert calls == [("edge", "es"), ("edge", "en")]


def test_voices_error_is_inline_json_and_not_cached(client, monkeypatch):
    calls = []

    def failing(provider, language, settings):
        calls.append(1)
        raise UserError("Edge TTS no pudo generar la voz (ClientError)", "Comprueba tu conexión a internet")

    monkeypatch.setattr(server.tts, "list_voices", failing)
    for _ in range(2):
        r = client.get("/api/voices?provider=edge&lang=es")
        assert r.status_code == 400 and "→" in r.json["error"]
    assert len(calls) == 2


def test_music_lists_moods_and_tracks(client):
    data = client.get("/api/music").json
    assert set(data["moods"]) >= {"lofi", "epico", "tranquilo", "energico"}
    assert data["tracks"] and {"id", "mood", "title", "value"} <= set(data["tracks"][0])


# ---- scripts and jobs ----

def test_error_shape(client):  # no keys configured
    r = client.post("/api/script", json={"topic": "x", "profile_id": "default"})
    assert r.status_code == 400 and "→" in r.json["error"]


def test_script_returns_script_json(client, monkeypatch):
    from baitd.models import Script
    seen = {}

    def fake(topic, profile, settings):
        seen["topic"], seen["profile"] = topic, profile.id
        return Script.from_dict(SCRIPT)

    monkeypatch.setattr(server.llm, "generate_script", fake)
    r = client.post("/api/script", json={"topic": "leer", "profile_id": "default"})
    assert r.status_code == 200 and r.json == SCRIPT and seen == {"topic": "leer", "profile": "default"}


def test_unexpected_error_is_sanitised(client, monkeypatch):
    def boom(*a):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(server.llm, "generate_script", boom)
    r = client.post("/api/script", json={"topic": "x", "profile_id": "default"})
    assert r.status_code == 500 and "→" in r.json["error"]
    assert "secret" not in r.text and "Traceback" not in r.text


def test_unknown_profile(client):
    r = client.post("/api/script", json={"topic": "x", "profile_id": "nope"})
    assert r.status_code == 400 and "→" in r.json["error"]


def test_jobs_flow(client):
    ids = client.post("/api/jobs", json={"topics": ["a", "b"], "profile_id": "default"}).json["ids"]
    assert len(ids) == 2
    jobs = client.get("/api/jobs").json["jobs"]
    assert [j["topic"] for j in jobs] == ["a", "b"] and {j["status"] for j in jobs} == {"pending"}
    assert client.post(f"/api/jobs/{ids[0]}/cancel", json={}).status_code == 200
    assert client.get("/api/jobs").json["jobs"][0]["status"] == "cancelled"
    assert client.delete(f"/api/jobs/{ids[0]}", json={}).status_code == 200
    assert [j["id"] for j in client.get("/api/jobs").json["jobs"]] == [ids[1]]
    client.post(f"/api/jobs/{ids[1]}/cancel", json={})
    client.post("/api/jobs/clear", json={})
    assert client.get("/api/jobs").json["jobs"] == []


@pytest.mark.parametrize("body", [{"topics": [], "profile_id": "default"},
                                  {"topics": "a", "profile_id": "default"},
                                  {"topics": ["a"], "profile_id": "default", "scripts": {"a": {"title": 1}}}])
def test_jobs_bad_input(client, body):
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 400 and "→" in r.json["error"]


def test_jobs_pass_reviewed_scripts(client, queue, runner):
    client.post("/api/jobs", json={"topics": ["a", "b"], "profile_id": "default", "scripts": {"a": SCRIPT}})
    queue.start()
    assert wait_for(lambda: len(runner.calls) == 2)
    scripts = dict(runner.calls)
    assert scripts["a"].to_dict() == SCRIPT and scripts["b"] is None


@pytest.mark.parametrize("topics", [["a"], ["a", "b"]])  # single job and batch
def test_reviewed_scene_1_edit_changes_hook_and_voiced_text(client, queue, runner, topics):
    # I4 / R28: there is no separate hook field; the server re-derives it from scene 1
    edited = {**SCRIPT, "hook": "Hola.",  # stale value from before the edit
              "scenes": [{"text": "¿Sabías esto? Mundo.", "keywords": ["a", "b"]}]}
    client.post("/api/jobs", json={"topics": topics, "profile_id": "default", "scripts": {"a": edited}})
    queue.start()
    assert wait_for(lambda: len(runner.calls) == len(topics))
    script = dict(runner.calls)["a"]
    assert script.hook == "¿Sabías esto?"
    assert script.scenes[0].text == "¿Sabías esto? Mundo."  # what the TTS voices


def test_job_video_streams_with_range(client, queue):
    id = finished_job(client, queue)
    job = client.get("/api/jobs").json["jobs"][-1]
    assert job["has_video"] is True
    with client.get(f"/api/jobs/{id}/video") as r:
        assert r.status_code == 200 and r.mimetype == "video/mp4" and len(r.data) == 4096
    with client.get(f"/api/jobs/{id}/video", headers={"Range": "bytes=0-99"}) as r:
        assert r.status_code == 206 and len(r.data) == 100
    assert client.get("/api/jobs/nope/video").status_code == 404


def test_video_route_rejects_cross_site_embedding(client, queue):
    # M16: a page on another site cannot embed/stream the local video
    id = finished_job(client, queue)
    r = client.get(f"/api/jobs/{id}/video", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and "→" in r.json["error"]
    for site in ("same-origin", "none"):
        with client.get(f"/api/jobs/{id}/video", headers={"Sec-Fetch-Site": site}) as ok:
            assert ok.status_code == 200


def test_open_folder_uses_job_output_only(client, queue, runner, monkeypatch):
    opened = []
    monkeypatch.setattr(server, "_open_folder", lambda p: opened.append(Path(p)))
    id = finished_job(client, queue)
    r = client.post(f"/api/jobs/{id}/open-folder", json={"path": "C:/Windows"})
    assert r.status_code == 200 and opened == [runner.root / "out-1"]
    assert client.post("/api/jobs/nope/open-folder", json={}).status_code == 404
    pending = client.post("/api/jobs", json={"topics": ["z"], "profile_id": "default"}).json["ids"][0]
    queue.stop()
    r = client.post(f"/api/jobs/{pending}/open-folder", json={})
    assert r.status_code in (400, 404) and "→" in r.json["error"] and len(opened) == 1


# ---- R22: local API hardening ----

@pytest.mark.parametrize("method,url", [
    ("post", "/api/settings"), ("post", "/api/profiles"), ("delete", "/api/profiles/default"),
    ("post", "/api/script"), ("post", "/api/jobs"), ("post", "/api/jobs/x/cancel"),
    ("post", "/api/jobs/x/open-folder"), ("post", "/api/jobs/clear"), ("delete", "/api/jobs/x"),
])
def test_mutations_require_json(client, method, url):
    for kwargs in ({"data": {"topic": "x"}}, {"data": "{}", "content_type": "text/plain"}, {}):
        r = getattr(client, method)(url, **kwargs)
        assert r.status_code == 415, (url, kwargs)
        assert "→" in r.json["error"]
    assert config.load_settings() == config.SETTINGS_DEFAULTS


@pytest.mark.parametrize("host", ["evil.com:5000", "127.0.0.1:5001", "localhost:5001", "127.0.0.1",
                                  "evil.com", "localhost.evil.com:5000"])
def test_foreign_host_rejected(client, host):
    for r in (client.get("/", headers={"Host": host}),
              client.get("/api/settings", headers={"Host": host}),
              client.get("/static/app.css", headers={"Host": host}),
              client.post("/api/settings", json={"theme": "light"}, headers={"Host": host})):
        assert r.status_code == 403
    assert config.load_settings()["theme"] == "dark"


def test_local_hosts_allowed(client):
    assert client.get("/api/settings", headers={"Host": "localhost:5000"}).status_code == 200
    assert client.get("/api/settings", headers={"Host": "127.0.0.1:5000"}).status_code == 200


def test_host_check_follows_server_port(app):
    c = app.test_client()
    assert c.get("/api/settings", base_url="http://127.0.0.1:8123").status_code == 200
    assert c.get("/api/settings", base_url="http://127.0.0.1:8123",
                 headers={"Host": "127.0.0.1:5000"}).status_code == 403


def test_no_cors_headers(client):
    origin = {"Origin": "https://evil.com"}
    for r in (client.get("/api/settings", headers=origin),
              client.options("/api/settings", headers={**origin, "Access-Control-Request-Method": "POST"}),
              client.post("/api/settings", json={}, headers=origin)):
        assert not [h for h in r.headers.keys() if h.lower().startswith("access-control-")]


# ---- fonts ----

def test_fonts_served(client):
    with client.get("/fonts/Anton-Regular.ttf") as r:
        head = r.data[:4]
        assert r.status_code == 200 and r.mimetype == "font/ttf"
    assert head in (b"\x00\x01\x00\x00", b"true", b"OTTO")


@pytest.mark.parametrize("path", ["/fonts/../captions.py", "/fonts/..%2Fcaptions.py", "/fonts/..%5Ccaptions.py",
                                  "/fonts/../../config.py", "/fonts/LICENSES/OFL-Anton.txt",
                                  "/fonts/missing.ttf", "/fonts/..", "/fonts/%2e%2e%2fconfig.py"])
def test_fonts_reject_traversal_and_non_ttf(client, path):
    assert client.get(path).status_code == 404


# ---- UI ----

def test_i18n_keys_match():
    es, en = json.loads(ES.read_text("utf-8")), json.loads(EN.read_text("utf-8"))
    assert set(es) == set(en)
    assert all(isinstance(v, str) and v for v in [*es.values(), *en.values()])


_I18N_ATTRS = {"placeholder": "data-i18n-placeholder", "aria-label": "data-i18n-aria", "title": "data-i18n-title"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _CopyChecker(HTMLParser):
    """Every text node with letters is exactly {{ t("key") }} inside an element with data-i18n="key"."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict]] = []
        self.problems: list[str] = []
        self.keys: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for attr, marker in _I18N_ATTRS.items():
            if attr in a:
                key = a.get(marker)
                if not key or a[attr] != f'{{{{ t("{key}") }}}}':
                    self.problems.append(f"<{tag} {attr}={a[attr]!r}>")
                else:
                    self.keys.add(key)
        if a.get("data-i18n"):
            self.keys.add(a["data-i18n"])
        if tag not in _VOID:
            self.stack.append((tag, a))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data):
        text = data.strip()
        if not text or not any(ch.isalpha() for ch in text):
            return
        tag, attrs = self.stack[-1] if self.stack else ("", {})
        if tag in ("script", "style"):
            return
        key = attrs.get("data-i18n")
        if not key or text != f'{{{{ t("{key}") }}}}':
            self.problems.append(f"<{tag}> {text!r}")


def test_index_has_no_hardcoded_copy():
    checker = _CopyChecker()
    checker.feed(INDEX.read_text("utf-8"))
    assert checker.problems == []
    es = json.loads(ES.read_text("utf-8"))
    assert checker.keys and checker.keys <= set(es)


def test_js_uses_known_i18n_keys_only():
    es = json.loads(ES.read_text("utf-8"))
    js = JS.read_text("utf-8")
    used = set(re.findall(r"""\btr\(\s*["']([\w.]+)["']""", js))
    assert used and used <= set(es), used - set(es)


def test_dynamic_i18n_keys_exist():
    from baitd import audio, captions, llm, tts
    from baitd.pipeline import STEPS
    es = json.loads(ES.read_text("utf-8"))
    keys = ({f"step.{s}" for s in STEPS} | {f"mood.{m}" for m in (*audio.MOODS, audio.USER_MOOD)}
            | {f"preset.{p}" for p in captions.PRESETS} | {f"pos.{p}" for p in ("bottom", "middle", "top")}
            | {f"voice.{p}" for p in tts.PROVIDERS} | {f"llm.{p}" for p in llm.DEFAULT_MODELS}
            | {f"status.{s}" for s in ("pending", "running", "done", "error", "cancelled")})
    assert keys <= set(es), keys - set(es)


def test_index_renders_offline_and_translated(client):
    r = client.get("/")
    assert r.status_code == 200 and r.mimetype == "text/html"
    html = r.text
    es = json.loads(ES.read_text("utf-8"))
    assert es["create"] in html and 'lang="es"' in html and "{{" not in html
    checker = _CopyChecker()
    checker.feed(html)  # parses; rendered copy replaces the expressions
    # offline: no external stylesheets, scripts or fonts
    for src in re.findall(r"""(?:src|href)=["']([^"']+)""", html):
        assert src.startswith(("/", "#")) or src.startswith(("https://www.pexels.com/api", "https://pixabay.com/api/docs",
                                                             "https://aistudio.google.com/app/apikey")), src
    css = CSS.read_text("utf-8")
    assert "http" not in css and "googleapis" not in html
    assert "#f2798f" not in (css + html + JS.read_text("utf-8")).lower()  # no coral
    for font in re.findall(r"url\(['\"]?/fonts/([^'\")]+)", css):
        with client.get(f"/fonts/{font}") as r:
            assert r.status_code == 200
    with client.get("/static/app.js") as r:
        assert r.status_code == 200
    with client.get("/static/i18n/en.json") as r:
        assert r.json == json.loads(EN.read_text("utf-8"))


def test_index_uses_saved_language_and_theme(client):
    client.post("/api/settings", json={"ui_lang": "en", "theme": "light"})
    html = client.get("/").text
    en = json.loads(EN.read_text("utf-8"))
    assert 'lang="en"' in html and 'data-theme="light"' in html and en["create"] in html


def test_index_theme_unset_on_first_run(client):
    assert 'data-theme=""' in client.get("/").text  # CSS follows prefers-color-scheme


def test_index_has_clipping_tab_and_drop_zone(client):
    html = client.get("/").text
    es = json.loads(ES.read_text("utf-8"))
    # two keyboard-usable tabs next to the logo, each with its own left panel
    assert 'role="tablist"' in html
    assert re.search(r'<button[^>]*role="tab"[^>]*id="tab-short"[^>]*aria-selected="true"', html)
    assert re.search(r'<button[^>]*role="tab"[^>]*id="tab-clip"[^>]*aria-selected="false"', html)
    assert 'id="panel-short" role="tabpanel"' in html and 'id="panel-clip" role="tabpanel"' in html
    assert es["tab.short"] in html and es["tab.clip"] in html
    # the drop zone: link input, file button, drag-and-drop area and the upload progress/cancel
    assert 'id="drop"' in html and es["clip.drop"] in html
    assert 'id="c-url"' in html and 'id="c-file"' in html and 'id="c-file-btn"' in html
    assert 'id="upload-cancel"' in html and 'id="upload-pct"' in html
    for field in ("c-count", "c-min", "c-max", "c-kind", "c-aspect", "c-fps", "c-transcriber", "c-model",
                  "c-language", "c-framing", "c-preset", "c-hook", "c-credit", "c-music"):
        assert f'id="{field}"' in html, field
    assert 'id="clip-find"' in html and 'id="clip-create"' in html
    assert 'id="clip-review"' in html and 'id="clip-export"' in html and 'id="clip-discard"' in html
    assert 'id="tpl-moment"' in html
    # settings dialog: keep sources and the yt-dlp updater
    assert 'id="s-keep_sources"' in html and 'id="ytdlp-update"' in html
    assert 'data-setting="yt_cookies_browser"' in html and 'data-setting="yt_cookies_file"' in html
    js = JS.read_text("utf-8")
    assert "new XMLHttpRequest()" in js and ".abort()" in js and '"X-Baitd-Upload", "1"' in js
    # chunked uploads (F3): 64 MiB PUTs in order, the extension checked first, abort deletes the partial upload
    up = js[js.index("// ---------- clipping: source"):js.index("function bindDrop")]
    assert "64 * 1024 * 1024" in up and 'xhr.open("PUT"' in up and "BOOT.upload_exts" in up
    assert 'api("DELETE", `/api/uploads/' in up and "/finish`" in up
    assert "/api/clips" in js and "/export" in js and "/api/ytdlp/update" in js


def test_clipping_ui_lifecycle_seams():
    js = JS.read_text("utf-8")
    run = js[js.index("async function onClipRun"):js.index("// ---------- clipping: review list")]
    assert "body.overrides = { ...state.current.clipping }" in run  # F7: what the screen shows, not a stale save
    save = js[js.index("function saveNow"):js.index("async function flushSave")]
    assert '$("#clip-error")' in save  # F7: save failures show on the Clipping tab too
    assert "orig: m.orig" in js  # F4: the moment's index in the analysis
    assert "`/api/sources/${encodeURIComponent(s.source_id)}`" in js  # F2: "Quitar archivo" deletes the upload
    discard = js[js.index("async function discardAnalysis"):js.index("async function onExport")]
    assert discard.index("setBusy(true)") < discard.index('api("DELETE"') < discard.index("setBusy(false)")


def test_index_has_rights_notice_key(client):
    html = client.get("/").text
    es = json.loads(ES.read_text("utf-8"))
    assert 'data-i18n="clip.rights"' in html and es["clip.rights"] in html
    client.post("/api/settings", json={"ui_lang": "en"})
    en = json.loads(EN.read_text("utf-8"))
    assert str(escape(en["clip.rights"])) in client.get("/").text  # "channels' videos" is HTML-escaped


# ---- shutdown (macOS .app has no console) ----

def test_shutdown_stops_queue_and_calls_hook(tmp_data, runner):
    called = threading.Event()
    q = JobQueue(tmp_data / "queue.json", runner=runner)
    q.start()
    a = server.create_app(q, shutdown=called.set)
    a.test_client_class = LocalClient
    r = a.test_client().post("/api/shutdown", json={})
    assert r.status_code == 200 and r.json == {"ok": True}
    assert called.wait(3)
    assert q._stopping  # the queue no longer accepts work


def test_shutdown_requires_json_and_local_host(tmp_data, runner):
    called = threading.Event()
    q = JobQueue(tmp_data / "queue.json", runner=runner)
    a = server.create_app(q, shutdown=called.set)
    a.test_client_class = LocalClient
    c = a.test_client()
    assert c.post("/api/shutdown", data="x", content_type="text/plain").status_code == 415
    assert c.post("/api/shutdown", json={}, base_url="http://evil.example:5000").status_code == 403
    assert c.get("/api/shutdown").status_code == 405
    assert not called.wait(0.5)


def test_quit_button_in_settings_dialog():
    html = INDEX.read_text("utf-8")
    assert 'id="quit-app"' in html
    assert "api/shutdown" in JS.read_text("utf-8")


def test_raw_settings_has_no_defaults(tmp_data):
    # M8: the stored values only, so the page knows whether a theme was ever chosen
    assert config.raw_settings() == {}
    config.save_settings({"ui_lang": "en"})
    assert config.raw_settings() == {"ui_lang": "en"}  # saving one key does not pin the defaults
    (tmp_data / "settings.json").write_text('{"theme": "light"}', encoding="utf-8")
    assert config.raw_settings() == {"theme": "light"}


def test_server_does_not_use_private_config_reader():
    assert "_read_json" not in (WEB / "server.py").read_text("utf-8")


def test_review_ui_has_remove_and_no_hook_field():
    # I4 (R28): no separate hook field; I8: a per-card "Quitar"
    html, js = INDEX.read_text("utf-8"), JS.read_text("utf-8")
    assert "script-hook" not in html and "script-hook" not in js and "script.hook" not in js
    assert 'class="link-btn script-remove" data-i18n="review.remove"' in html
    assert "state.review.splice(" in js and "state.review = null" in js
    es, en = json.loads(ES.read_text("utf-8")), json.loads(EN.read_text("utf-8"))
    assert es["review.remove"] == "Quitar" and en["review.remove"] == "Remove"


def test_small_ui_items_are_wired():
    html, js = INDEX.read_text("utf-8"), JS.read_text("utf-8")
    assert '<a class="skip" href="#head"' in html and 'id="head" tabindex="-1"' in html  # M16
    assert 'id="fps-hint"' in html and '$("#fps-hint").hidden = p.fps !== 60' in js  # M7
    es, en = json.loads(ES.read_text("utf-8")), json.loads(EN.read_text("utf-8"))
    assert es["fps.hint"] == "60 fps puede tardar más" and en["fps.hint"] == "60 fps may take longer"
    assert 'id="rate-note"' in html and '$("#f-rate").disabled = noSpeed' in js  # M6
    assert "if (body.theme === dlg.dataset.themeShown) delete body.theme" in js  # M8
    assert '$("#profile").disabled = busy' in js and '$("#profile-menu-btn").disabled = busy' in js  # M9
    assert "const previous = state.savePromise" in js  # M9: saves are chained


# ---- Task 2: AI provider and model in Ajustes ----

def test_settings_llm_provider_and_model_validation(client):
    assert client.post("/api/settings", json={"llm_provider": "zzz"}).status_code == 400
    r = client.post("/api/settings", json={"llm_model": "m" * 81})
    assert r.status_code == 400 and "→" in r.json["error"]
    assert config.load_settings()["llm_provider"] == "gemini"
    r = client.post("/api/settings", json={"llm_provider": "openai", "llm_model": "m" * 80})
    assert r.status_code == 200 and r.json["llm_provider"] == "openai"
    assert config.load_settings()["llm_model"] == "m" * 80


def test_profile_without_llm_fields_is_valid(client):
    p = client.get("/api/profiles").json["profiles"][0]
    assert "llm_provider" not in p and "llm_model" not in p
    assert client.post("/api/profiles", json=dict(p, llm_provider="zzz")).status_code == 200  # ignored


def test_llm_models_route(client, monkeypatch):
    seen = {}

    def fake(provider, settings):
        seen["provider"], seen["key"] = provider, settings["gemini_key"]
        return ["gemini-3.6-flash"]

    monkeypatch.setattr(server.llm, "list_models", fake)
    client.post("/api/settings", json={"gemini_key": "g-secret-1234"})
    r = client.get("/api/llm/models?provider=gemini")
    assert r.status_code == 200
    assert r.json == {"models": ["gemini-3.6-flash"], "default": "gemini-3.6-flash"}
    assert seen == {"provider": "gemini", "key": "g-secret-1234"} and "g-secret" not in r.text
    assert client.get("/api/llm/models?provider=zzz").status_code == 400
    assert client.get("/api/llm/models").status_code == 400


def test_llm_models_route_without_key_is_a_400(client):
    r = client.get("/api/llm/models?provider=gemini")
    assert r.status_code == 400 and r.json["error"] == "Falta la clave de Gemini → Añádela en Ajustes"


def test_settings_dialog_has_the_ai_block(client):
    html = client.get("/").text
    dialog = html[html.index('<dialog id="settings"'):]
    assert 'data-setting="llm_provider"' in dialog and 'data-setting="llm_model"' in dialog
    assert 'id="s-llm_model_pick"' in dialog and 'id="llm-models-refresh"' in dialog and 'id="llm-hint"' in dialog
    assert 'id="f-llm"' not in html and 'id="f-model"' not in html
    assert dialog.index('data-setting="compat_key"') < dialog.index('data-setting="llm_provider"') < dialog.index('data-setting="output_dir"')


def test_clipping_form_help_toggles_and_visible_min_max(client):
    html = client.get("/").text
    es = json.loads(ES.read_text("utf-8"))
    en = json.loads((ES.parent / "en.json").read_text("utf-8"))
    for key in ("count", "len"):
        assert f'id="c-{key}-help"' in html and f'aria-controls="c-{key}-help"' in html
        assert re.search(rf'<p[^>]*class="hint"[^>]*id="c-{key}-help"[^>]*\bhidden\b', html)
    assert re.search(r'<button[^>]*type="button"[^>]*id="c-count-help-btn"[^>]*aria-expanded="false"', html)
    assert 'data-i18n-aria="clip.count_help_label"' in html
    assert re.search(r'role="group"[^>]*aria-labelledby="c-len-label"', html)
    assert 'class="sr-only" for="c-min"' not in html and 'class="sr-only" for="c-max"' not in html
    assert (es["clip.min"], es["clip.max"]) == ("mín", "máx")
    assert (en["clip.min"], en["clip.max"]) == ("min", "max")
    assert es["progress.step_pct"] == "{step} · {n} %" and en["progress.step_pct"] == "{step} · {n} %"
    for k in ("clip.count_help_label", "clip.count_help", "clip.len_help"):
        assert es[k] and en[k]


def test_boot_carries_version_and_page_shows_it(client):
    import baitd
    html = client.get("/").text
    assert baitd.__version__ == "0.4.0"
    assert f'"version": "{baitd.__version__}"' in html
    assert 'id="app-version"' in html
    es = json.loads(ES.read_text("utf-8"))
    en = json.loads(EN.read_text("utf-8"))
    assert es["settings.version"] == "Versión {v}" and en["settings.version"] == "Version {v}"
    assert 'tr("settings.version"' in JS.read_text("utf-8")


def test_version_matches_installed_metadata():
    from importlib.metadata import version

    import baitd
    assert version("baitd") == baitd.__version__


def test_favicon_and_brand_icon(client):
    html = client.get("/").text
    assert '<link rel="icon" type="image/png" href="/static/icon.png">' in html
    assert re.search(r'<img class="brand-icon" src="/static/icon.png" alt="" width="28" height="28">', html)
    assert html.index('class="brand-icon"') < html.index('class="brand"')
    assert client.get("/static/icon.png").status_code == 200



# ---- updates ----

def test_check_updates_setting_is_boolean(client):
    assert client.get("/api/settings").json["check_updates"] is True
    assert client.post("/api/settings", json={"check_updates": False}).status_code == 200
    assert client.get("/api/settings").json["check_updates"] is False
    assert client.post("/api/settings", json={"check_updates": "no"}).status_code == 400


def test_api_update_shape(client, monkeypatch):
    from baitd import __version__, updater
    monkeypatch.setattr(updater, "state", {"latest": None})
    assert client.get("/api/update").json == {"current": __version__, "latest": None, "can_install": False}
    latest = {"version": "9.9.9", "page": "https://github.com/okksett/baitd/releases/tag/v9.9.9",
              "setup_url": "", "sums_url": ""}
    updater.state["latest"] = latest
    assert client.get("/api/update").json == {"current": __version__, "latest": latest, "can_install": False}


def test_install_409_when_not_installable(client, monkeypatch):
    from baitd import updater
    monkeypatch.setattr(updater, "state", {"latest": {"version": "9.9.9", "setup_url": "", "sums_url": ""}})
    monkeypatch.setattr(updater, "download_and_verify", lambda *a, **k: pytest.fail("must not download"))
    r = client.post("/api/update/install", json={})
    assert r.status_code == 409 and "→" in r.json["error"]


def test_install_happy_path_order(queue, monkeypatch, tmp_data):
    from baitd import updater
    calls = []
    latest = {"version": "9.9.9", "page": "p", "setup_url": "u", "sums_url": "s"}
    monkeypatch.setattr(updater, "state", {"latest": latest})
    monkeypatch.setattr(updater, "can_install", lambda l: True)
    monkeypatch.setattr(updater, "download_and_verify",
                        lambda l, dest, **k: calls.append(("download", dest.name)) or dest / "s.exe")
    monkeypatch.setattr(updater, "launch_installer", lambda p: calls.append(("launch", p.name)))
    app = server.create_app(queue, shutdown=lambda: calls.append(("shutdown",)))
    app.test_client_class = LocalClient
    r = app.test_client().post("/api/update/install", json={})
    assert r.status_code == 200 and r.json == {"ok": True}
    assert wait_for(lambda: ("shutdown",) in calls)
    assert calls == [("download", "actualizaciones"), ("launch", "s.exe"), ("shutdown",)]


def test_install_failed_verification_does_not_launch(client, monkeypatch):
    from baitd import updater
    monkeypatch.setattr(updater, "state", {"latest": {"version": "9.9.9", "setup_url": "u", "sums_url": "s"}})
    monkeypatch.setattr(updater, "can_install", lambda l: True)

    def bad(*a, **k):
        raise UserError("No se pudo verificar la actualización", "Descárgala desde la página de Baitd")

    monkeypatch.setattr(updater, "download_and_verify", bad)
    monkeypatch.setattr(updater, "launch_installer", lambda p: pytest.fail("must not launch"))
    r = client.post("/api/update/install", json={})
    assert r.status_code == 400 and "verificar" in r.json["error"]


def test_install_disk_error_is_a_user_error(client, monkeypatch):
    from baitd import updater
    monkeypatch.setattr(updater, "state", {"latest": {"version": "9.9.9", "setup_url": "u", "sums_url": "s"}})
    monkeypatch.setattr(updater, "can_install", lambda l: True)
    monkeypatch.setattr(updater, "download_and_verify", lambda *a, **k: (_ for _ in ()).throw(
        UserError("No se pudo guardar la actualización",
                  "Libera espacio en el disco o descárgala desde la página de Baitd")))
    r = client.post("/api/update/install", json={})
    assert r.status_code == 400
    assert r.json["error"] == ("No se pudo guardar la actualización → "
                               "Libera espacio en el disco o descárgala desde la página de Baitd")


def test_install_after_successful_launch_gets_409(queue, monkeypatch, tmp_data):
    from baitd import updater
    calls = []
    monkeypatch.setattr(updater, "installing", False)
    monkeypatch.setattr(updater, "state", {"latest": {"version": "9.9.9", "setup_url": "u", "sums_url": "s"}})
    monkeypatch.setattr(updater, "can_install", lambda l: True)
    monkeypatch.setattr(updater, "download_and_verify", lambda l, dest, **k: calls.append("download") or dest / "s.exe")
    monkeypatch.setattr(updater, "launch_installer", lambda p: calls.append("launch"))
    app = server.create_app(queue, shutdown=lambda: None)
    app.test_client_class = LocalClient
    assert app.test_client().post("/api/update/install", json={}).status_code == 200
    r2 = app.test_client().post("/api/update/install", json={})  # inside the 0.3 s before shutdown
    assert r2.status_code == 409 and "Ya se está descargando la actualización" in r2.json["error"]
    assert calls == ["download", "launch"]


def test_second_concurrent_install_gets_409(queue, monkeypatch, tmp_data):
    from baitd import updater
    started, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(updater, "state", {"latest": {"version": "9.9.9", "setup_url": "u", "sums_url": "s"}})
    monkeypatch.setattr(updater, "can_install", lambda l: True)

    def slow(l, dest, **k):
        calls.append("download")
        started.set()
        assert release.wait(5)
        return dest / "s.exe"

    monkeypatch.setattr(updater, "download_and_verify", slow)
    monkeypatch.setattr(updater, "launch_installer", lambda p: calls.append("launch"))
    app = server.create_app(queue)
    app.test_client_class = LocalClient
    first = {}
    t = threading.Thread(target=lambda: first.update(r=app.test_client().post("/api/update/install", json={})))
    t.start()
    assert started.wait(3)
    r2 = app.test_client().post("/api/update/install", json={})
    assert r2.status_code == 409 and "Ya se está descargando la actualización" in r2.json["error"]
    assert calls == ["download"]  # the second request did not touch the first
    release.set()
    t.join(5)
    assert first["r"].status_code == 200 and calls == ["download", "launch"]
    assert updater.install_lock.acquire(blocking=False)  # released afterwards
    updater.install_lock.release()
