"""clip.voices: telling two voices apart per sentence (synthetic voices: two different timbres)."""
import threading
import types
import wave

import numpy as np
import pytest

from baitd.clip import pipeline, voices
from baitd.models import Word

RATE = voices.RATE


def _voice(kind: str, seconds: float, seed: int) -> np.ndarray:
    """A crude 'voice': a low buzzy one (harmonics of 110 Hz) or a bright one (harmonics of 240 Hz with a
    high formant), with syllable-like loudness changes so each sentence has louder and quieter frames."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * RATE)) / RATE
    f0, bright = (110.0, 0.3) if kind == "low" else (240.0, 1.0)
    sig = sum(np.sin(2 * np.pi * f0 * h * t + rng.uniform(0, 6)) * (bright ** (h - 1) if h > 1 else 1.0) / h
              for h in range(1, 12))
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t + rng.uniform(0, 6))
    return (0.3 * sig * envelope + 0.01 * rng.normal(size=len(t))).astype(np.float32)


def test_two_timbres_split():
    two = [voices.voiceprint(_voice("low" if i % 2 else "high", 2.0, i)) for i in range(8)]
    labels, sep = voices.separate(two)
    assert None not in labels and sep >= voices.MIN_SEPARATION
    assert labels[1::2] == [labels[1]] * 4 and labels[0::2] == [labels[0]] * 4 and labels[0] != labels[1]


def test_a_lone_odd_sentence_is_not_a_second_voice():
    prints = [voices.voiceprint(_voice("low", 2.0, i)) for i in range(9)] + [voices.voiceprint(_voice("high", 2.0, 99))]
    assert voices.separate(prints)[0] == [None] * 10  # 1 in 10 is under MIN_SHARE


def test_too_short_or_too_few_sentences_give_no_voices():
    assert voices.voiceprint(np.zeros(int(0.5 * RATE), dtype=np.float32)) is None
    few = [voices.voiceprint(_voice("low" if i else "high", 2.0, i)) for i in range(3)]
    assert voices.separate(few)[0] == [None] * 3
    assert voices.separate([None, None, None, None, None])[0] == [None] * 5


def test_sentence_voices_reads_the_file(tmp_path):
    parts, spans, t = [], [], 0.0
    for i in range(6):
        parts += [_voice("low" if i % 2 else "high", 1.6, i), np.zeros(int(0.4 * RATE), dtype=np.float32)]
        spans.append((t, t + 1.6))
        t += 2.0
    path = tmp_path / "talk.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.concatenate(parts) * 32767).astype(np.int16).tobytes())
    labels, _ = voices.sentence_voices(path, spans)
    assert labels[0] == labels[2] == labels[4] != labels[1] == labels[3] == labels[5]


def test_sentence_voices_stops_on_cancel(tmp_path):
    ev = threading.Event()
    ev.set()
    from baitd.ffmpeg import JobCancelled
    with pytest.raises(JobCancelled):
        voices.sentence_voices(tmp_path / "x.wav", [(i, i + 1.0) for i in range(5)], ev)


# ---- pipeline: which sentences and voices reach the crop ----

def _words(n=40, sentence=5):
    """Words every 0.5 s; every `sentence`-th ends a sentence."""
    return [Word(f"w{i}." if i % sentence == sentence - 1 else f"w{i}", i * 0.5, i * 0.5 + 0.4) for i in range(n)]


def test_talk_and_voices_uses_the_sentences_around_the_clip(monkeypatch):
    seen = {}

    def fake(path, spans, cancel=None):
        seen["spans"] = spans
        return [i % 2 for i in range(len(spans))], 0.3

    monkeypatch.setattr(voices, "sentence_voices", fake)
    src = types.SimpleNamespace(path="x.mp4")
    m = types.SimpleNamespace(start=5.0, end=10.0, title="t")
    talk, labels = pipeline._talk_and_voices(src, m, _words(), "cara", threading.Event())
    assert len(seen["spans"]) == 8  # the whole 20 s: context on both sides of the clip
    assert talk[0][0] <= 0 and talk[-1][1] >= 5.0 - 0.6  # clip-relative, the edge sentences included
    assert len(labels) == len(talk) and set(labels) == {0, 1}


def test_talk_and_voices_skips_voices_outside_the_face_crop_and_on_errors(monkeypatch):
    src = types.SimpleNamespace(path="x.mp4")
    m = types.SimpleNamespace(start=5.0, end=10.0, title="t")
    monkeypatch.setattr(voices, "sentence_voices", lambda *a, **k: pytest.fail("not needed for a centred crop"))
    talk, labels = pipeline._talk_and_voices(src, m, _words(), "centro", threading.Event())
    assert talk and labels is None

    def boom(*a, **k):
        raise RuntimeError("no audio stream")
    monkeypatch.setattr(voices, "sentence_voices", boom)
    talk, labels = pipeline._talk_and_voices(src, m, _words(), "cara", threading.Event())
    assert talk and labels is None  # mouth motion still follows the speaker
