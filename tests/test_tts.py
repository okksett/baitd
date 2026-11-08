import base64
import json
import re
import subprocess

import pytest
import requests

from baitd import tts
from baitd.config import default_profile
from baitd.ffmpeg import ffmpeg_exe, probe
from baitd.models import Scene, Script, UserError, Word
from baitd.tts.timing import (
    align_to_script, chars_to_words, edge_boundaries_to_words, estimate_words, scene_bounds,
)


def script(*texts):
    return Script("t", texts[0], [Scene(t, ["a", "b"]) for t in texts], "d", [], "es")


def profile(**kw):
    p = default_profile()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def make_wav(path, seconds):
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", "-f", "lavfi", "-i", f"sine=d={seconds}",
                    str(path)], check=True, capture_output=True)


def sample_rate(path):
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return int(re.search(r"(\d+) Hz", r.stderr).group(1))


# ---- timing ----

def test_edge_boundaries_to_words():
    w = edge_boundaries_to_words([{"offset": 1_000_000, "duration": 4_750_000, "text": "Sabías"}])
    assert w == [Word("Sabías", 0.1, 0.575)]


def test_chars_to_words():
    w = chars_to_words("hola mundo", [0, .1, .2, .3, .4, .5, .6, .7, .8, .9],
                       [.1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0])
    assert [(x.text, x.start, x.end) for x in w] == [("hola", 0, .4), ("mundo", .5, 1.0)]


def test_estimate_words_covers_span():
    w = estimate_words("uno dos tres", 1.0, 2.2)
    assert w[0].start == 1.0 and w[-1].end == 2.2 and len(w) == 3


def test_scene_bounds_cover_total():
    words = [Word("a", 0.1, 0.4), Word("b", 0.5, 0.9), Word("c", 1.2, 1.5)]
    assert scene_bounds([2, 1], words, 2.0) == [(0.0, 1.2), (1.2, 2.0)]


def test_align_restores_punctuation():
    timed = [Word("Sabías", 0.1, 0.5), Word("que", 0.5, 0.7), Word("estres", 0.7, 1.2)]
    out = align_to_script(["¿Sabías", "que", "estrés?"], timed)
    assert [w.text for w in out] == ["¿Sabías", "que", "estrés?"]
    assert [(w.start, w.end) for w in out] == [(0.1, 0.5), (0.5, 0.7), (0.7, 1.2)]


def test_align_merges_unmatched_tokens_from_neighbours():
    # "68%" is spoken as three words; "—" is not spoken at all.
    timed = [Word("el", 0, .2), Word("sixty", .2, .5), Word("eight", .5, .8), Word("percent", .8, 1.2),
             Word("de", 1.2, 1.4), Word("todos", 1.4, 2.0)]
    tokens = ["el", "68%", "—", "de", "todos"]
    out = align_to_script(tokens, timed)
    assert [w.text for w in out] == tokens
    assert out[0].end == 0.2 and out[3].start == 1.2
    starts = [w.start for w in out]
    assert starts == sorted(starts)
    assert all(w.end >= w.start for w in out)
    assert 0.2 <= out[1].start and out[2].end <= 1.2


def _timed(text):
    return [Word(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(text.split())]


def test_align_long_expansion_does_not_break_following_tokens():
    tokens = "Sube un 68% de todos los casos hoy mismo".split()
    timed = _timed("Sube un sesenta y ocho por ciento de todos los casos hoy mismo")
    out = align_to_script(tokens, timed)
    assert [w.text for w in out] == tokens
    by_text = {w.text: w for w in timed}
    for w in out[3:]:
        assert abs(w.start - by_text[w.text].start) < 0.05
        assert abs(w.end - by_text[w.text].end) < 0.05
    # "68%" spans the five spoken words
    assert out[2].start == pytest.approx(timed[2].start) and out[2].end == pytest.approx(timed[6].end)


def test_align_contraction_two_tokens_one_timed_word():
    tokens = ["voy", "a", "la", "casa", "del", "rio"]
    timed = [Word("voy", 0, .3), Word("a", .3, .4), Word("la", .4, .6), Word("casa", .6, 1.0),
             Word("delrio", 1.0, 1.6)]
    out = align_to_script(tokens, timed)
    assert [w.text for w in out] == tokens
    assert out[4].start == 1.0 and out[5].end == 1.6 and out[4].end == pytest.approx(out[5].start)


def test_align_starts_and_ends_non_decreasing():
    tokens = "Sube un 68% — de todos los casos hoy".split()
    timed = _timed("Sube un sesenta y ocho por ciento de todos los casos hoy")
    out = align_to_script(tokens, timed)
    assert [w.start for w in out] == sorted(w.start for w in out)
    assert [w.end for w in out] == sorted(w.end for w in out)
    assert all(w.end >= w.start for w in out)
    assert all(out[i].end <= out[i + 1].start + 1e-9 for i in range(len(out) - 1))


def test_align_falls_back_to_estimate_when_poor():
    timed = [Word("xx", 0, 1), Word("yy", 1, 2)]
    out = align_to_script(["uno", "dos", "tres", "cuatro"], timed)
    assert [w.text for w in out] == ["uno", "dos", "tres", "cuatro"]
    assert out[0].start == 0 and out[-1].end == 2


def test_align_with_no_timed_uses_span():
    out = align_to_script(["a", "b"], [], span=(0.0, 1.0))
    assert len(out) == 2 and out[-1].end == 1.0


# ---- synthesize ----

def test_words_are_monotonic_after_concat(tmp_path, monkeypatch):
    durations = {"uno dos": 1.0, "tres cuatro cinco": 1.5, "seis": 0.5}

    def fake(text, profile, settings, out):
        make_wav(out, durations[text])
        return [Word(t, i * 0.2, i * 0.2 + 0.2) for i, t in enumerate(text.split())]

    monkeypatch.setitem(tts.PROVIDERS, "fake", fake)
    s = script("uno dos", "tres cuatro cinco", "seis")
    vo = tts.synthesize(s, profile(tts_provider="fake"), {}, tmp_path)
    assert vo.path == tmp_path / "voice.wav" and vo.path.exists()
    assert probe(vo.path)["duration"] == pytest.approx(3.0, abs=0.05)
    assert [w.text for w in vo.words] == "uno dos tres cuatro cinco seis".split()
    starts = [w.start for w in vo.words]
    assert starts == sorted(starts) and all(w.end >= w.start for w in vo.words)
    b = vo.scene_bounds
    assert b[0][0] == 0 and b[-1][1] == pytest.approx(vo.duration)
    assert all(b[i][1] == b[i + 1][0] for i in range(len(b) - 1))
    assert b[1][0] == pytest.approx(1.0, abs=0.05)
    assert vo.words[2].start >= b[1][0] - 1e-6  # scene 2's first word is shifted by scene 1's length
    assert sample_rate(vo.path) == 48000


def test_provider_without_timings_is_estimated(tmp_path, monkeypatch):
    def fake(text, profile, settings, out):
        make_wav(out, 1.0)
        return None

    monkeypatch.setitem(tts.PROVIDERS, "fake", fake)
    vo = tts.synthesize(script("hola mundo"), profile(tts_provider="fake"), {}, tmp_path)
    assert [w.text for w in vo.words] == ["hola", "mundo"]
    assert vo.words[-1].end == pytest.approx(vo.duration, abs=0.01)


@pytest.mark.parametrize("provider,key", [("openai", "openai_key"), ("elevenlabs", "elevenlabs_key")])
def test_missing_key_raises_before_http(provider, key, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no HTTP expected")

    monkeypatch.setattr(requests, "post", boom)
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(UserError):
        tts.synthesize(script("hola"), profile(tts_provider=provider), {key: ""}, tmp_path)


class FakeResp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content
        self.text = json.dumps(body or {})

    def json(self):
        return self._body


def test_openai_speed_key_header_and_whisper_words(tmp_path, monkeypatch):
    wav = tmp_path / "src.wav"
    make_wav(wav, 1.0)
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if url.endswith("/audio/speech"):
            return FakeResp(content=wav.read_bytes())
        return FakeResp(body={"words": [{"word": "Hola", "start": 0.0, "end": 0.4},
                                         {"word": "mundo", "start": 0.5, "end": 0.9}]})

    monkeypatch.setattr(requests, "post", post)
    vo = tts.synthesize(script("¡Hola mundo!"), profile(tts_provider="openai", voice="nova", rate=50),
                        {"openai_key": "sk-test"}, tmp_path)
    url, kw = calls[0]
    assert kw["json"]["model"] == "tts-1-hd" and kw["json"]["speed"] == 1.5
    assert kw["json"]["voice"] == "nova"
    assert kw["headers"]["Authorization"] == "Bearer sk-test"
    assert "sk-test" not in url
    assert [w.text for w in vo.words] == ["¡Hola", "mundo!"]


def test_openai_whisper_failure_falls_back_to_estimate(tmp_path, monkeypatch):
    wav = tmp_path / "src.wav"
    make_wav(wav, 1.0)

    def post(url, **kw):
        if url.endswith("/audio/speech"):
            return FakeResp(content=wav.read_bytes())
        return FakeResp(500, {})

    monkeypatch.setattr(requests, "post", post)
    vo = tts.synthesize(script("hola mundo"), profile(tts_provider="openai", voice="nova"),
                        {"openai_key": "k"}, tmp_path)
    assert [w.text for w in vo.words] == ["hola", "mundo"]


def test_elevenlabs_with_timestamps(tmp_path, monkeypatch):
    wav = tmp_path / "src.wav"
    make_wav(wav, 1.0)
    text = "¡Hola mundo!"
    n = len(text)
    body = {"audio_base64": base64.b64encode(wav.read_bytes()).decode(),
            "alignment": {"characters": list(text),
                          "character_start_times_seconds": [i / n for i in range(n)],
                          "character_end_times_seconds": [(i + 1) / n for i in range(n)]}}
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return FakeResp(body=body)

    monkeypatch.setattr(requests, "post", post)
    vo = tts.synthesize(script(text), profile(tts_provider="elevenlabs", voice="VID"),
                        {"elevenlabs_key": "el-key"}, tmp_path)
    assert seen["url"].endswith("/v1/text-to-speech/VID/with-timestamps")
    assert seen["headers"]["xi-api-key"] == "el-key" and "el-key" not in seen["url"]
    assert [w.text for w in vo.words] == ["¡Hola", "mundo!"]


# ---- voices ----

def test_list_voices_filters_language(monkeypatch):
    import edge_tts

    async def fake():
        return [
            {"ShortName": "fr-FR-DeniseNeural", "FriendlyName": "Denise", "Gender": "Female", "Locale": "fr-FR"},
            {"ShortName": "fr-CA-JeanNeural", "FriendlyName": "Jean", "Gender": "Male", "Locale": "fr-CA"},
            {"ShortName": "es-ES-ElviraNeural", "FriendlyName": "Elvira", "Gender": "Female", "Locale": "es-ES"},
        ]

    monkeypatch.setattr(edge_tts, "list_voices", fake)
    v = tts.list_voices("edge", "fr", {})
    assert [x["id"] for x in v] == ["fr-FR-DeniseNeural", "fr-CA-JeanNeural"]
    assert all(x["free"] is True for x in v)
    assert set(v[0]) == {"id", "name", "gender", "free"}


def test_list_voices_openai_static():
    assert "nova" in [v["id"] for v in tts.list_voices("openai", "es", {})]


def test_list_voices_elevenlabs(monkeypatch):
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return FakeResp(body={"voices": [{"voice_id": "v1", "name": "Rachel", "labels": {"gender": "female"}}]})

    monkeypatch.setattr(requests, "get", get)
    v = tts.list_voices("elevenlabs", "es", {"elevenlabs_key": "k"})
    assert v == [{"id": "v1", "name": "Rachel", "gender": "female", "free": False}]
    assert seen["headers"]["xi-api-key"] == "k"
    with pytest.raises(UserError):
        tts.list_voices("elevenlabs", "es", {})


@pytest.mark.network
def test_edge_real_synthesis(tmp_path):
    s = script("¿Sabías que leer reduce el estrés?", "Empieza hoy con diez páginas.")
    vo = tts.synthesize(s, profile(tts_provider="edge", voice="es-ES-ElviraNeural"), {}, tmp_path)
    assert vo.path.exists() and vo.duration > 1
    assert [w.text for w in vo.words][:2] == ["¿Sabías", "que"]
    assert len(vo.words) == 11
    assert all(w.end > w.start for w in vo.words)
    assert len(vo.scene_bounds) == 2


def test_elevenlabs_unknown_voice_says_voz(tmp_path, monkeypatch):
    # M6: a 404 there is about the voice id, not a model
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResp(status=404))
    with pytest.raises(UserError) as e:
        tts.synthesize(script("hola"), profile(tts_provider="elevenlabs", voice="VID"),
                       {"elevenlabs_key": "k"}, tmp_path)
    assert e.value.cause == "La voz 'VID' no existe en ElevenLabs"
    assert e.value.fix == "Cambia la voz en el perfil"
