import json

import pytest

from baitd.clip import moments
from baitd.live import ai
from baitd.llm import _http
from baitd.models import UserError, Word


def _reply(**kw):
    data = {"tipo": "humor", "inicio": "0:05", "fin": "0:40", "titulo": "Se cae de la silla",
            "descripcion": "Una caída", "hashtags": ["risa", "#directo"], **kw}
    return "```json\n" + json.dumps(data) + "\n```"


def test_prompt_asks_json_and_span_only_when_wanted():
    lines = "[00:01] hola a todos.\n[00:04] mirad esto."
    with_span = ai.build_prompt(lines, "es", want_span=True)
    without = ai.build_prompt(lines, "es", want_span=False)
    for p in (with_span, without):
        assert "JSON" in p and lines in p and "es" in p
        assert '"tipo"' in p and '"titulo"' in p and '"descripcion"' in p and '"hashtags"' in p
        assert f"at most {moments.TITLE_WORDS} words" in p
    assert '"inicio"' in with_span and '"fin"' in with_span
    assert '"inicio"' not in without and '"fin"' not in without


def test_parse_reply():
    v = ai.parse_reply(_reply(), 100.0, 160.0, want_span=True)
    assert v.start == 105 and v.end == 140
    assert v.kind == "gracioso" and v.title == "Se cae de la silla" and v.description == "Una caída"
    assert v.hashtags == ["#risa", "#directo"]
    assert ai.parse_reply(_reply(tipo="polemica"), 100.0, 160.0, want_span=True).kind == "polemico"
    assert ai.parse_reply(_reply(tipo="nada"), 100.0, 160.0, want_span=True).kind == "nada"
    clamped = ai.parse_reply(_reply(inicio="0:00", fin="5:00"), 100.0, 160.0, want_span=True)
    assert (clamped.start, clamped.end) == (100.0, 160.0)
    long_title = ai.parse_reply(_reply(titulo=" ".join(["uno"] * 20)), 100.0, 160.0, want_span=True)
    assert len(long_title.title.split()) == moments.TITLE_WORDS
    no_span = ai.parse_reply(_reply(), 100.0, 160.0, want_span=False)
    assert no_span.start is None and no_span.end is None and no_span.title == "Se cae de la silla"
    for garbage in ("no json at all", "{broken", "[1, 2]", _reply(tipo="otro"), _reply(titulo=3)):
        assert ai.parse_reply(garbage, 100.0, 160.0, want_span=True) is None


def test_parse_reply_short_span_uses_window():
    v = ai.parse_reply(_reply(inicio="0:05", fin="0:08"), 100.0, 160.0, want_span=True)
    assert (v.start, v.end) == (100.0, 160.0)
    v = ai.parse_reply(_reply(inicio="0:30", fin="0:10"), 100.0, 160.0, want_span=True)
    assert (v.start, v.end) == (100.0, 160.0)


def test_hourly_limit():
    now = [1000.0]
    limit = ai.HourlyLimit(6, clock=lambda: now[0])
    assert limit.left() == 6
    assert [limit.take() for _ in range(6)] == [True] * 6
    assert limit.take() is False and limit.left() == 0
    now[0] += 3600.0
    assert limit.take() is True and limit.left() == 5
    limit.exhaust()
    assert limit.left() == 0
    now[0] += 10 * 3600.0
    assert limit.take() is False and limit.left() == 0


def test_lines_are_relative_to_the_window():
    words = [Word("hola.", 101.0, 101.5), Word("mirad", 104.2, 104.6), Word("esto.", 104.7, 105.0),
             Word("fuera.", 170.0, 170.4)]
    assert ai.window_lines(words, 100.0, 160.0) == "[00:01] hola.\n[00:04] mirad esto."


def test_exhausted_quota_is_its_own_error(monkeypatch):
    class Resp:
        status_code = 429
        headers = {}
        text = ""

        def json(self):
            return {"error": {"code": "insufficient_quota"}}

    monkeypatch.setattr(_http.requests, "post", lambda *a, **k: Resp())
    with pytest.raises(_http.QuotaExhausted) as e:
        _http.request("post", "https://example.test/v1", "OpenAI", "gpt-x", retry=True)
    assert isinstance(e.value, UserError) and "Se acabó la cuota de OpenAI" in str(e.value)
