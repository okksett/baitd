import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from baitd.clip import transcribe
from baitd.ffmpeg import JobCancelled
from baitd.models import UserError

KEY = "sk-secret-123"


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = "body"

    def json(self):
        return self._payload


def _words(n=25, base=0.0):
    return [{"word": f"w{i}", "start": base + i, "end": base + i + 0.5} for i in range(n)]


def _patch_post(monkeypatch, handler):
    calls = []

    def post(url, timeout=None, **kw):
        calls.append({"url": url, **kw})
        return handler(len(calls), kw)

    monkeypatch.setattr(transcribe.requests, "post", post)
    return calls


def _openai(wav, language=None, cancel=None, on_progress=None):
    return transcribe.openai_words(wav, language, {"openai_key": KEY},
                                   cancel or threading.Event(), on_progress or (lambda p: None))


def test_chunk_constant():
    assert transcribe.CHUNK_S == 600


def test_openai_chunks_and_offsets(monkeypatch, make_wav):
    monkeypatch.setattr(transcribe, "CHUNK_S", 60.0)
    wav = make_wav(seconds=150)
    calls = _patch_post(monkeypatch, lambda n, kw: FakeResp(200, {"language": "english", "words": _words()}))
    progress = []
    t = _openai(wav, on_progress=progress.append)
    assert len(calls) == 3
    assert calls[0]["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {KEY}"
    data = dict(calls[0]["data"])
    assert data["model"] == "whisper-1" and data["response_format"] == "verbose_json"
    assert data["timestamp_granularities[]"] == "word" and "language" not in data
    assert len(t.words) == 75 and t.language == "en"
    assert t.words[0].start == 0 and t.words[25].start == 60 and t.words[50].start == 120
    assert [w.start for w in t.words] == sorted(w.start for w in t.words)
    assert t.duration == pytest.approx(150, abs=1)
    assert progress == pytest.approx([1 / 3, 2 / 3, 1.0])


def test_openai_language_sent_and_used(monkeypatch, make_wav):
    wav = make_wav(seconds=5)
    calls = _patch_post(monkeypatch, lambda n, kw: FakeResp(200, {"language": "spanish", "words": _words()}))
    t = _openai(wav, language="es")
    assert dict(calls[0]["data"])["language"] == "es" and t.language == "es"


def test_openai_missing_key_message(make_wav):
    with pytest.raises(UserError) as e:
        transcribe.openai_words(make_wav(seconds=2), None, {}, threading.Event(), lambda p: None)
    assert e.value.cause == "Falta la clave de OpenAI"
    assert e.value.fix == "Ajústala en Ajustes o usa la transcripción local"


def test_key_never_in_error_text(monkeypatch, make_wav):
    _patch_post(monkeypatch, lambda n, kw: FakeResp(401))
    with pytest.raises(UserError) as e:
        _openai(make_wav(seconds=2))
    assert "sk-" not in str(e.value) and KEY not in str(e.value)


def test_openai_cleans_chunk_files(monkeypatch, make_wav):
    wav = make_wav(seconds=5)
    seen = []

    def handler(n, kw):
        seen.append(kw["files"]["file"][1].name)
        return FakeResp(200, {"language": "en", "words": _words()})

    _patch_post(monkeypatch, handler)
    _openai(wav)
    assert seen and not Path(seen[0]).exists()


def _fake_model(words_per_seg=10, segs=3, lang="en", duration=30.0, on_seg=None):
    def segments():
        for s in range(segs):
            if on_seg:
                on_seg(s)
            ws = [NS(word=f" w{s}_{i}" if i else f"  w{s}_{i},", start=s * 10 + i * 0.5, end=s * 10 + i * 0.5 + 0.4)
                  for i in range(words_per_seg)]
            ws.append(NS(word="  ", start=s * 10 + 9, end=s * 10 + 9.1))
            yield NS(end=(s + 1) * 10.0, words=ws)

    class M:
        def transcribe(self, path, **kw):
            M.kw = kw
            return segments(), NS(language=lang, duration=duration)
    return M


def test_local_uses_fake_model_and_progress(monkeypatch, make_wav):
    M = _fake_model()
    loaded = []
    monkeypatch.setattr(transcribe, "_load_model", lambda m: loaded.append(m) or M())
    progress = []
    t = transcribe.local_words(make_wav(seconds=30), None, "small", threading.Event(), progress.append)
    assert loaded == ["small"]
    assert M.kw["word_timestamps"] is True and M.kw["vad_filter"] is True and M.kw["language"] is None
    assert len(t.words) == 30 and t.language == "en" and t.duration == 30.0
    assert t.words[0].text == "w0_0," and t.words[1].text == "w0_1"
    assert progress == pytest.approx([1 / 3, 2 / 3, 1.0])


def test_local_model_failure_maps_to_user_error(monkeypatch, make_wav):
    def boom(m):
        raise OSError("net down")
    monkeypatch.setattr(transcribe, "_load_model", boom)
    with pytest.raises(UserError) as e:
        transcribe.local_words(make_wav(seconds=2), None, "small", threading.Event(), lambda p: None)
    assert e.value.cause == "No se pudo descargar el modelo de transcripción"
    assert e.value.fix == "Revisa tu conexión e inténtalo de nuevo"
    assert isinstance(e.value.__cause__, OSError)


def test_no_speech_raises(monkeypatch, make_wav):
    wav = make_wav(seconds=5)
    monkeypatch.setattr(transcribe, "_load_model", lambda m: _fake_model(words_per_seg=3, segs=2)())
    with pytest.raises(UserError) as e:
        transcribe.local_words(wav, None, "small", threading.Event(), lambda p: None)
    assert e.value.cause == "No se encontraron momentos con habla en este video"
    assert e.value.fix == "Prueba con otro video o elige el idioma a mano"
    _patch_post(monkeypatch, lambda n, kw: FakeResp(200, {"language": "en", "words": _words(5)}))
    with pytest.raises(UserError, match="habla"):
        _openai(wav)


def test_cancel_between_chunks_and_segments(monkeypatch, make_wav):
    monkeypatch.setattr(transcribe, "CHUNK_S", 60.0)
    wav = make_wav(seconds=150)
    cancel = threading.Event()

    def handler(n, kw):
        cancel.set()
        return FakeResp(200, {"language": "en", "words": _words()})

    calls = _patch_post(monkeypatch, handler)
    with pytest.raises(JobCancelled):
        _openai(wav, cancel=cancel)
    assert len(calls) == 1

    cancel2 = threading.Event()
    monkeypatch.setattr(transcribe, "_load_model",
                        lambda m: _fake_model(on_seg=lambda s: cancel2.set() if s == 1 else None)())
    with pytest.raises(JobCancelled):
        transcribe.local_words(wav, None, "small", cancel2, lambda p: None)


def test_dispatch_and_models_dir(monkeypatch, make_wav, tmp_data):
    assert transcribe.models_dir().name == "modelos"
    monkeypatch.setattr(transcribe, "_load_model", lambda m: _fake_model()())
    t = transcribe.transcribe(make_wav(seconds=3), "local", None, {}, "small", threading.Event(), lambda p: None)
    assert len(t.words) == 30
    with pytest.raises(ValueError):
        transcribe.transcribe(make_wav(seconds=3), "nope", None, {}, "small", threading.Event(), lambda p: None)
