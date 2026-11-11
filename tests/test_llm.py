import json
import logging
import threading

import pytest
import requests

from baitd.config import default_profile
from baitd.llm import build_prompt, generate_script, parse_script
from baitd import llm
from baitd.ffmpeg import JobCancelled
from baitd.llm import _http
from baitd.llm._http import AuthError, NotFoundError
from baitd.models import UserError

VALID = json.dumps({
    "title": "Leer cambia tu mente",
    "hook": "¿Sabías que leer 6 minutos reduce el estrés?",
    "scenes": [
        {"text": "¿Sabías que leer 6 minutos reduce el estrés?", "keywords": ["person reading", "calm"]},
        {"text": "Mejora tu memoria. Y tu vocabulario.", "keywords": ["brain", "library", "books"]},
        {"text": "Empieza hoy con diez páginas.", "keywords": ["open book", "coffee"]},
    ],
    "description": "Beneficios de leer.",
    "hashtags": ["#lectura", "#habitos"],
})
NO_KEYWORDS = json.dumps({
    "title": "t", "hook": "h",
    "scenes": [{"text": "a"}, {"text": "b"}, {"text": "c"}],
    "description": "d", "hashtags": [],
})


def profile(**kw):
    p = default_profile()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def openai_reply(content):
    return FakeResp(200, {"choices": [{"message": {"content": content}}]})


def gemini_reply(content):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": content}]}}]})


SETTINGS = {"openai_key": "sk-x", "gemini_key": "g-x", "compat_base_url": "http://localhost:11434/v1",
            "compat_key": ""}


def test_prompt_contains_rules():
    p = build_prompt("los beneficios de leer", profile(language="es", seconds=45, tone="tono sorprendido"))
    assert "es" in p and "112" in p and "tono sorprendido" in p and "keywords" in p
    assert "los beneficios de leer" in p and "English" in p


def long_hook(hook_field="h"):
    data = json.loads(VALID)
    data["hook"] = hook_field
    data["scenes"][0]["text"] = ("Esta es una frase de gancho demasiado larga que no se dice en tres segundos. "
                                 "Sigue la escena.")
    return json.dumps(data)


def test_prompt_caps_the_hook_at_8_words():
    p = build_prompt("leer", profile())
    assert "at most 8 words" in p and "under 3 seconds" in p


def test_parse_script_derives_hook_from_scene_1():
    data = json.loads(VALID)
    data["hook"] = "Otra cosa que no se dice"
    data["scenes"][0]["text"] = "Leer te cambia. Y mucho."
    assert parse_script(json.dumps(data), "es").hook == "Leer te cambia."
    del data["hook"]  # the field is optional: scene 1 is the single source of truth (R28)
    assert parse_script(json.dumps(data), "es").hook == "Leer te cambia."


def test_parse_script_rejects_a_long_hook():
    with pytest.raises(ValueError, match="hook"):
        parse_script(long_hook(), "es")
    assert len(parse_script(long_hook(), "es", strict_hook=False).hook.split()) > 10


def test_long_hook_triggers_the_repair_retry(monkeypatch):
    calls = []
    replies = iter([openai_reply(long_hook()), openai_reply(VALID)])
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append(kw) or next(replies))
    s = generate_script("leer", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert len(calls) == 2 and s.hook == "¿Sabías que leer 6 minutos reduce el estrés?"
    assert "hook" in calls[1]["json"]["messages"][0]["content"].split("rejected:")[1]


def test_still_long_hook_after_retry_is_accepted_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(requests, "post", lambda url, **kw: openai_reply(long_hook()))
    with caplog.at_level(logging.WARNING, logger="baitd.llm"):
        s = generate_script("leer", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert len(s.hook.split()) > 10 and s.scenes[0].text.startswith(s.hook)
    assert "gancho" in caplog.text


def test_parse_script_with_fences():
    s = parse_script("Aquí va:\n```json\n" + VALID + "\n```", "es")
    assert s.hook and len(s.scenes) >= 3 and s.language == "es"
    assert s.scenes[1].keywords == ["brain", "library", "books"]


def test_parse_script_rejects_missing_keywords():
    with pytest.raises(ValueError):
        parse_script(NO_KEYWORDS, "es")


def test_parse_script_rejects_prose():
    with pytest.raises(ValueError):
        parse_script("lo siento, no puedo", "es")


def test_generate_retries_once(monkeypatch):
    calls = []
    replies = iter([openai_reply("prosa sin json"), openai_reply(VALID)])

    def post(url, **kw):
        calls.append((url, kw))
        return next(replies)

    monkeypatch.setattr(requests, "post", post)
    s = generate_script("leer", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert s.title and s.language == "es" and len(calls) == 2
    assert "valid JSON" in calls[1][1]["json"]["messages"][0]["content"]
    assert calls[0][0] == "https://api.openai.com/v1/chat/completions"
    assert calls[0][1]["json"]["model"] == "gpt-4o-mini"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sk-x"
    assert calls[0][1]["timeout"]


def test_generate_gives_up_after_second_failure(monkeypatch):
    n = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: n.append(1) or openai_reply("nope"))
    with pytest.raises(UserError):
        generate_script("leer", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert len(n) == 2


def test_gemini_request_shape(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return gemini_reply(VALID)

    monkeypatch.setattr(requests, "post", post)
    s = generate_script("leer", profile(language="fr"), {**SETTINGS, "llm_provider": "gemini"})
    assert s.language == "fr"
    assert seen["url"].endswith("/models/gemini-3.6-flash:generateContent")
    assert "g-x" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "g-x"
    assert seen["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_compat_uses_base_url_and_optional_key(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return openai_reply(VALID)

    monkeypatch.setattr(requests, "post", post)
    generate_script("leer", profile(), {**SETTINGS, "llm_provider": "compat", "llm_model": "qwen2"})
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in seen["headers"]
    assert seen["json"]["model"] == "qwen2"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_provider_http_error_message(monkeypatch, provider):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, {"error": "bad"}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": provider})
    assert "clave" in str(e.value) and "Ajustes" in str(e.value)


def test_gemini_invalid_key_400(monkeypatch):
    body = {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid",
                      "details": [{"reason": "API_KEY_INVALID"}]}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(400, body))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "gemini"})
    assert "clave" in str(e.value)


def test_model_not_found(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(404, {}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai", "llm_model": "gpt-zzz"})
    assert "'gpt-zzz'" in e.value.cause and "OpenAI" in e.value.cause and "Ajustes" in e.value.fix


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(429, {}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert "límite" in str(e.value)


def test_connection_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "gemini"})
    assert "Sin conexión con Gemini" in str(e.value)


def test_timeout(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert "tardó demasiado" in str(e.value)


def test_server_error(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(500, {}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai"})
    assert "500" in str(e.value)


def test_missing_key_raises_user_error(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HTTP called")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "openai_key": "", "llm_provider": "openai"})
    assert "Ajustes" in str(e.value)
    with pytest.raises(UserError):
        generate_script("x", profile(), {**SETTINGS, "gemini_key": "", "llm_provider": "gemini"})
    with pytest.raises(UserError):
        generate_script("x", profile(), {**SETTINGS, "compat_base_url": "", "llm_provider": "compat"})


def test_unknown_provider():
    with pytest.raises(UserError):
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "zzz"})


def test_unexpected_response_shape(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {"weird": 1}))
    with pytest.raises(UserError):
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai"})


# ---- M1 / M5: typed errors and compat wording ----

def test_auth_failures_are_typed(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, {}))
    with pytest.raises(AuthError):
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai"})


def test_compat_wording_uses_del(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, {}))
    with pytest.raises(AuthError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat"})
    assert e.value.cause == "La clave del servidor compatible no es válida"


def test_compat_connection_refused_suggests_ollama(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat"})
    assert e.value.cause == "Sin conexión con el servidor compatible"
    assert e.value.fix == "¿Está abierto Ollama / el servidor?"


def test_compat_404_suggests_v1(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(404, {}))
    with pytest.raises(NotFoundError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat", "llm_model": "qwen2"})
    assert e.value.cause == "El modelo 'qwen2' no existe en el servidor compatible"
    assert e.value.fix == "Revisa la URL (¿termina en /v1?) o el modelo"


def test_compat_timeout_message_is_capitalised(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat"})
    assert e.value.cause == "El servidor compatible tardó demasiado en responder"


def test_default_gemini_model_is_current():
    assert llm.DEFAULT_MODELS["gemini"] == "gemini-3.6-flash"


def test_request_retries_503_then_succeeds(monkeypatch):
    replies = iter([FakeResp(503), FakeResp(503), FakeResp(200, {"ok": 1})])
    delays = []
    monkeypatch.setattr(_http, "_sleep", lambda seconds, cancel=None: delays.append(seconds))
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(replies))
    resp = _http.request("post", "http://x", "Gemini", "m", retry=True)
    assert resp.status_code == 200
    assert delays == [5, 15]


def test_request_gives_up_after_three_retries(monkeypatch):
    calls, delays = [], []
    monkeypatch.setattr(_http, "_sleep", lambda seconds, cancel=None: delays.append(seconds))
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(503))
    with pytest.raises(UserError) as e:
        _http.request("post", "http://x", "Gemini", "m", retry=True)
    assert "saturado" in e.value.cause
    assert "Reintentar" in e.value.fix
    assert delays == [5, 15, 30]
    assert len(calls) == 4


def test_request_keeps_rate_limit_message_after_retries(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(429))
    with pytest.raises(UserError) as e:
        _http.request("post", "http://x", "Gemini", "m", retry=True)
    assert "límite de peticiones" in e.value.cause


def gemini_429(quota_id: str, retry_delay: str, value: str = "20"):
    """Shape of a real Gemini 429 (captured 2026-11-10), minus the help link."""
    return FakeResp(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
            {"quotaId": quota_id, "quotaValue": value,
             "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
    ]}})


def test_daily_quota_is_not_retried_and_says_so(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or gemini_429(
        "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "84005s"))
    with pytest.raises(UserError) as e:
        _http.request("post", "http://x", "Gemini", "gemini-3.6-flash", retry=True)
    assert len(calls) == 1
    assert e.value.cause == ("Se acabó la cuota de Gemini para el modelo 'gemini-3.6-flash' "
                             "(20 peticiones al día en el plan gratis)")
    assert "otro modelo en Ajustes" in e.value.fix


def test_per_minute_quota_waits_what_gemini_asks(monkeypatch):
    replies = iter([gemini_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "37.2s"),
                    FakeResp(200, {"ok": 1})])
    delays = []
    monkeypatch.setattr(_http, "_sleep", lambda seconds, cancel=None: delays.append(seconds))
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(replies))
    assert _http.request("post", "http://x", "Gemini", "m", retry=True).status_code == 200
    assert delays == [39]


def test_long_retry_delay_is_not_waited(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(429, {"error": {
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]}}))
    with pytest.raises(UserError) as e:
        _http.request("post", "http://x", "Gemini", "m", retry=True)
    assert len(calls) == 1 and e.value.cause.startswith("Se acabó la cuota de Gemini")


def test_openai_insufficient_quota_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(
        429, {"error": {"code": "insufficient_quota", "type": "insufficient_quota", "message": "x"}}))
    with pytest.raises(UserError) as e:
        _http.request("post", "http://x", "OpenAI", "gpt-4o-mini", retry=True)
    assert len(calls) == 1
    assert e.value.cause == "Se acabó la cuota de OpenAI para el modelo 'gpt-4o-mini'"


@pytest.mark.parametrize("body", [
    {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}, "usageMetadata": {"promptTokenCount": 7993}},
    {"candidates": [{"finishReason": "SAFETY", "content": {}}]},
])
def test_gemini_content_filter_is_typed(monkeypatch, body):
    from baitd.llm import gemini
    from baitd.llm._http import ContentBlocked

    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, body))
    with pytest.raises(ContentBlocked) as e:
        gemini.complete("x", "gemini-3.6-flash", {"gemini_key": "k"})
    assert "filtro de contenido" in e.value.cause and "otro proveedor" in e.value.fix


def _ollama_up(monkeypatch, reply=None, status=200):
    from baitd.llm import ollama
    monkeypatch.setattr(ollama, "_probe", lambda root: root == "http://localhost:11434")
    sent = []

    def post(url, **kw):
        sent.append((url, kw))
        return FakeResp(status, reply if reply is not None else {"message": {"content": VALID}})
    monkeypatch.setattr(requests, "post", post)
    return sent


def test_compat_address_that_is_ollama_uses_its_native_api(monkeypatch):
    sent = _ollama_up(monkeypatch)
    s = generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat", "llm_model": "qwen2.5"})
    assert s.title == "Leer cambia tu mente"
    url, kw = sent[0]
    assert url == "http://localhost:11434/api/chat"  # /v1 dropped: the native API lives at the root
    body = kw["json"]
    assert body["model"] == "qwen2.5" and body["stream"] is False and body["format"] == "json"
    assert body["options"]["num_ctx"] >= 4096
    assert kw["timeout"] == 900  # a local model may need minutes


def test_ollama_context_fits_a_20_minute_clipping_window(monkeypatch):
    from baitd.llm import ollama
    window = "x" * 23610  # a real 20 min window prompt: 23,610 chars, ~8,600 tokens
    assert ollama.context_for(window) >= 8600 + 2048
    assert ollama.context_for(window) % 1024 == 0
    assert ollama.context_for("hola") == 4096 and ollama.context_for("x" * 10 ** 6) == 32768
    sent = _ollama_up(monkeypatch, {"message": {"content": "{}"}})
    llm.complete(window, profile(), {**SETTINGS, "llm_provider": "compat"})
    assert sent[0][1]["json"]["options"]["num_ctx"] == ollama.context_for(window)


def test_ollama_is_detected_once_and_other_servers_keep_v1(monkeypatch):
    from baitd.llm import ollama
    probes = []
    monkeypatch.setattr(ollama, "_probe", lambda root: probes.append(root) or root.endswith(":11434"))
    urls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: urls.append(url) or (
        FakeResp(200, {"message": {"content": "{}"}}) if "/api/chat" in url else openai_reply("{}")))
    for _ in range(2):
        llm.complete("p", profile(), {**SETTINGS, "llm_provider": "compat"})
    llm.complete("p", profile(), {**SETTINGS, "llm_provider": "compat", "compat_base_url": "http://lmstudio:1234/v1"})
    assert urls == ["http://localhost:11434/api/chat"] * 2 + ["http://lmstudio:1234/v1/chat/completions"]
    assert probes.count("http://localhost:11434") == 1  # a yes is remembered


def test_ollama_cloud_keeps_the_v1_path(monkeypatch):
    from baitd.llm import ollama
    monkeypatch.setattr(ollama, "_probe", lambda root: True)  # ollama.com does answer /api/version
    urls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: urls.append(url) or openai_reply("{}"))
    llm.complete("p", profile(), {**SETTINGS, "llm_provider": "compat", "compat_base_url": "https://ollama.com/v1",
                                  "compat_key": "k"})
    assert urls == ["https://ollama.com/v1/chat/completions"]


def test_ollama_missing_model_says_how_to_get_it(monkeypatch):
    _ollama_up(monkeypatch, {"error": "model 'qwen9' not found"}, status=404)
    with pytest.raises(NotFoundError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat", "llm_model": "qwen9"})
    assert "qwen9" in e.value.cause and e.value.fix == "Descárgalo con: ollama pull qwen9"


def test_cancel_does_not_wait_for_a_slow_ollama(monkeypatch):
    import time
    from baitd.llm import ollama
    monkeypatch.setattr(ollama, "_found", {"http://localhost:11434"})
    monkeypatch.setattr(ollama, "CANCEL_POLL_S", 0.05)
    release = threading.Event()

    def slow_post(*a, **k):
        release.wait(5)  # a local model still thinking
        return FakeResp(200, {"message": {"content": "{}"}})
    monkeypatch.setattr(requests, "post", slow_post)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(JobCancelled):
        llm.complete("p", profile(), {**SETTINGS, "llm_provider": "compat"}, cancel=cancel)
    assert time.monotonic() - t0 < 2
    release.set()


def test_ollama_errors_reach_the_caller_through_the_worker_thread(monkeypatch):
    _ollama_up(monkeypatch, {"error": "model 'm' not found"}, status=404)
    with pytest.raises(NotFoundError):
        llm.complete("p", profile(), {**SETTINGS, "llm_provider": "compat", "llm_model": "m"},
                     cancel=threading.Event())


def test_ollama_down_says_to_open_it(monkeypatch):
    from baitd.llm import ollama
    monkeypatch.setattr(ollama, "_found", {"http://localhost:11434"})  # was up a moment ago

    def boom(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "compat"})
    assert e.value.cause == "Sin conexión con Ollama" and e.value.fix == "¿Está abierto Ollama?"


@pytest.mark.parametrize("status", [404, 401])
def test_request_does_not_retry_404_or_401(monkeypatch, status):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(status))
    with pytest.raises(UserError):
        _http.request("post", "http://x", "Gemini", "m", retry=True)
    assert len(calls) == 1


def test_request_retry_wait_stops_on_cancel(monkeypatch):
    cancel = threading.Event()
    calls = []
    monkeypatch.setattr(_http, "_sleep", lambda seconds, c=None: cancel.set())
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(503))
    with pytest.raises(JobCancelled):
        _http.request("post", "http://x", "Gemini", "m", retry=True, cancel=cancel)
    assert len(calls) == 1


def test_request_without_retry_makes_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(1) or FakeResp(503))
    with pytest.raises(UserError) as e:
        _http.request("post", "http://x", "Pexels", "m")
    assert len(calls) == 1
    assert e.value.cause == "Pexels respondió con el error 503"


# ---- Task 2: provider and model come from the settings ----

def test_complete_uses_settings_provider_and_model(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return openai_reply("ok")

    monkeypatch.setattr(requests, "post", post)
    p = profile()
    assert not hasattr(p, "llm_provider") and not hasattr(p, "llm_model")
    out = llm.complete("hola", p, {"llm_provider": "openai", "llm_model": "gpt-x", "openai_key": "k"})
    assert out == "ok"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions" and seen["json"]["model"] == "gpt-x"


def test_complete_defaults_to_gemini_default_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(requests, "post", lambda url, **kw: seen.update(url=url) or gemini_reply("ok"))
    llm.complete("hola", profile(), {"gemini_key": "g"})
    assert seen["url"].endswith("/models/gemini-3.6-flash:generateContent")


def fake_get(monkeypatch, body, status=200):
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return FakeResp(status, body)

    monkeypatch.setattr(requests, "get", get)
    return seen


def test_list_models_gemini_filters_and_strips(monkeypatch):
    seen = fake_get(monkeypatch, {"models": [
        {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemma-3-27b", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.6-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.6-flash-image", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.6-flash-native-audio", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.6-flash-live", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-exp", "supportedGenerationMethods": ["generateContent"]},
    ]})
    out = llm.list_models("gemini", {"gemini_key": "g-secret"})
    assert out == ["gemini-3.5-flash-lite", "gemini-3.6-flash"]
    assert seen["headers"]["x-goog-api-key"] == "g-secret" and "g-secret" not in seen["url"]
    assert "pageSize=1000" in seen["url"]


def test_list_models_openai_filters(monkeypatch):
    ids = ["gpt-4o", "gpt-4o-mini", "o3-mini", "o1", "chatgpt-4o-latest", "gpt-4o-audio-preview",
           "gpt-4o-realtime-preview", "gpt-4o-transcribe", "gpt-4o-mini-tts", "gpt-image-1",
           "gpt-4o-search-preview", "text-embedding-3-small", "whisper-1", "dall-e-3", "o4-mini"]
    seen = fake_get(monkeypatch, {"data": [{"id": i} for i in ids]})
    assert llm.list_models("openai", {"openai_key": "sk-x"}) == [
        "chatgpt-4o-latest", "gpt-4o", "gpt-4o-mini", "o1", "o3-mini", "o4-mini"]
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer sk-x"


def test_list_models_compat_returns_all_ids(monkeypatch):
    seen = fake_get(monkeypatch, {"data": [{"id": "qwen2"}, {"id": "llama3.1"}]})
    out = llm.list_models("compat", {"compat_base_url": "http://localhost:11434/v1/", "compat_key": ""})
    assert out == ["llama3.1", "qwen2"]
    assert seen["url"] == "http://localhost:11434/v1/models" and "Authorization" not in seen["headers"]
    llm.list_models("compat", {"compat_base_url": "http://x/v1", "compat_key": "kk"})
    assert seen["headers"]["Authorization"] == "Bearer kk"


def test_list_models_requires_key(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HTTP called")

    monkeypatch.setattr(requests, "get", boom)
    for provider, settings in (("gemini", {}), ("openai", {}), ("compat", {"compat_base_url": ""})):
        with pytest.raises(UserError) as e:
            llm.list_models(provider, settings)
        assert "Ajustes" in e.value.fix
    with pytest.raises(UserError):
        llm.list_models("zzz", {})


def test_list_models_makes_a_single_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(1) or FakeResp(503, {}))
    with pytest.raises(UserError):
        llm.list_models("openai", {"openai_key": "k"})
    assert len(calls) == 1


def test_list_models_bad_key_and_odd_payload(monkeypatch):
    fake_get(monkeypatch, {}, status=401)
    with pytest.raises(AuthError):
        llm.list_models("openai", {"openai_key": "k"})
    fake_get(monkeypatch, {"weird": 1})
    with pytest.raises(UserError):
        llm.list_models("gemini", {"gemini_key": "k"})


def test_not_found_fix_points_to_ajustes(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(404, {}))
    with pytest.raises(NotFoundError) as e:
        generate_script("x", profile(), {**SETTINGS, "llm_provider": "openai", "llm_model": "gpt-zzz"})
    assert e.value.fix == "Cambia el modelo en Ajustes"
