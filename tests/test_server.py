import json
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask.testing import FlaskClient

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

    def __call__(self, topic, profile, settings, script, cancel, progress):
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


def test_masked_value_not_overwritten(client):
    client.post("/api/settings", json={"pexels_key": "abcdef1234"})
    masked = client.get("/api/settings").json["pexels_key"]
    r = client.post("/api/settings", json={"pexels_key": masked, "output_dir": "C:/videos"})
    assert r.status_code == 200
    assert config.load_settings()["pexels_key"] == "abcdef1234"
    assert config.load_settings()["output_dir"] == "C:/videos"


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


def test_cannot_delete_last_profile(client):
    client.get("/api/profiles")
    r = client.delete("/api/profiles/default", json={})
    assert r.status_code == 400 and "→" in r.json["error"]
    assert [p.id for p in config.list_profiles()] == ["default"]


@pytest.mark.parametrize("bad", [{"name": ""}, {"rate": 99}, {"music_volume": 2}, {"fps": 24},
                                 {"music": "C:/Windows/notepad.exe"},
                                 {"subtitle": {"position": "left"}}])
def test_invalid_profile_rejected(client, bad):
    base = client.get("/api/profiles").json["profiles"][0]
    r = client.post("/api/profiles", json={**base, **bad})
    assert r.status_code == 400 and "→" in r.json["error"]


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


def test_job_video_streams_with_range(client, queue):
    id = finished_job(client, queue)
    job = client.get("/api/jobs").json["jobs"][-1]
    assert job["has_video"] is True
    with client.get(f"/api/jobs/{id}/video") as r:
        assert r.status_code == 200 and r.mimetype == "video/mp4" and len(r.data) == 4096
    with client.get(f"/api/jobs/{id}/video", headers={"Range": "bytes=0-99"}) as r:
        assert r.status_code == 206 and len(r.data) == 100
    assert client.get("/api/jobs/nope/video").status_code == 404


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
