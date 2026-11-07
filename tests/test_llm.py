import json

import pytest
import requests

from baitd.config import default_profile
from baitd.llm import build_prompt, generate_script, parse_script
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
    s = generate_script("leer", profile(llm_provider="openai"), SETTINGS)
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
        generate_script("leer", profile(llm_provider="openai"), SETTINGS)
    assert len(n) == 2


def test_gemini_request_shape(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return gemini_reply(VALID)

    monkeypatch.setattr(requests, "post", post)
    s = generate_script("leer", profile(llm_provider="gemini", language="fr"), SETTINGS)
    assert s.language == "fr"
    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert "g-x" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "g-x"
    assert seen["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_compat_uses_base_url_and_optional_key(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return openai_reply(VALID)

    monkeypatch.setattr(requests, "post", post)
    generate_script("leer", profile(llm_provider="compat", llm_model="qwen2"), SETTINGS)
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in seen["headers"]
    assert seen["json"]["model"] == "qwen2"


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_provider_http_error_message(monkeypatch, provider):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, {"error": "bad"}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider=provider), SETTINGS)
    assert "clave" in str(e.value) and "Ajustes" in str(e.value)


def test_gemini_invalid_key_400(monkeypatch):
    body = {"error": {"status": "INVALID_ARGUMENT", "message": "API key not valid",
                      "details": [{"reason": "API_KEY_INVALID"}]}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(400, body))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="gemini"), SETTINGS)
    assert "clave" in str(e.value)


def test_model_not_found(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(404, {}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="openai", llm_model="gpt-zzz"), SETTINGS)
    assert "'gpt-zzz'" in e.value.cause and "OpenAI" in e.value.cause and "perfil" in e.value.fix


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(429, {}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="openai"), SETTINGS)
    assert "límite" in str(e.value)


def test_connection_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="gemini"), SETTINGS)
    assert "Sin conexión con Gemini" in str(e.value)


def test_timeout(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="openai"), SETTINGS)
    assert "tardó demasiado" in str(e.value)


def test_server_error(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(500, {}))
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="openai"), SETTINGS)
    assert "500" in str(e.value)


def test_missing_key_raises_user_error(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HTTP called")

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UserError) as e:
        generate_script("x", profile(llm_provider="openai"), {**SETTINGS, "openai_key": ""})
    assert "Ajustes" in str(e.value)
    with pytest.raises(UserError):
        generate_script("x", profile(llm_provider="gemini"), {**SETTINGS, "gemini_key": ""})
    with pytest.raises(UserError):
        generate_script("x", profile(llm_provider="compat"), {**SETTINGS, "compat_base_url": ""})


def test_unknown_provider():
    with pytest.raises(UserError):
        generate_script("x", profile(llm_provider="zzz"), SETTINGS)


def test_unexpected_response_shape(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {"weird": 1}))
    with pytest.raises(UserError):
        generate_script("x", profile(llm_provider="openai"), SETTINGS)
