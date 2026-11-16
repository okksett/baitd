import threading
import time

import pytest

from baitd import ffmpeg
from baitd.live import signals
from baitd.live.signals import LAUGH, AudioSignal, ChatSignal, LoudnessMeter, Spike, segment_loudness


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_laugh_regex():
    for text in ["jajaja", "JSJSJS", "kekw", "😂", "lmaooo", "JAJA", "jajajaj que risa", "xD", "XDDD", "xdxd",
                 "lol", "LMAO", "hahaha", "kkkkk", "LUL", "OMEGALUL", "ICANT", "🤣", "💀", "[emote:39261:KEKW]"]:
        assert LAUGH.search(text), text
    for text in ["javier", "hola", "lolita", "ja", "hoy jugamos", "max dps", "jamás", "kk"]:
        assert not LAUGH.search(text), text


def _baseline(sig, seconds=120, per_bin=1):
    """`per_bin` messages in each 10 s bin from t=0 to `seconds`."""
    for b in range(int(seconds // signals.BIN_S)):
        for i in range(per_bin):
            sig.add(b * signals.BIN_S + 1 + i * 0.1, "hola")


def test_chat_spike_and_cooldown():
    c = ChatSignal()
    _baseline(c)
    assert c.check(120.0) is None
    for i in range(12):
        c.add(121 + i * 0.5, "que dijo")
    sp = c.check(130.0)
    assert sp == Spike(t=120.0, reason="chat", strength=12.0)
    assert c.check(130.4) is None  # the same bin is judged once
    for i in range(12):
        c.add(131 + i * 0.5, "otra vez")
    assert c.check(140.0) is None  # cooldown


def test_laughs_win():
    c = ChatSignal()
    _baseline(c)
    for i in range(10):
        c.add(121 + i * 0.5, "JAJAJA" if i < 7 else "que")
    sp = c.check(130.0)
    assert sp is not None and sp.reason == "laughs" and sp.t == 120.0


def test_no_spike_before_history():
    c = ChatSignal()
    assert c.check(5.0) is None  # nothing yet
    _baseline(c, seconds=30)
    for i in range(20):
        c.add(31 + i * 0.3, "jajaja")
    assert c.check(40.0) is None


def test_quiet_chat_needs_enough_messages():
    c = ChatSignal()
    _baseline(c, per_bin=0)  # no messages at all, then a few
    c.add(1.0, "hola")
    for i in range(signals.CHAT_MIN_MSGS - 1):
        c.add(121 + i, "eh")
    assert c.check(130.0) is None


def test_audio_spike():
    a = AudioSignal()
    for i in range(40):  # 80 s at -30
        a.add(i * 2.0, -30.0)
    assert a.check() is None
    a.add(80.0, -20.0)
    sp = a.check()
    assert sp == Spike(t=80.0, reason="loud", strength=10.0)
    assert a.check() is None
    a.add(82.0, -19.0)
    assert a.check() is None  # cooldown


def test_audio_needs_history():
    a = AudioSignal()
    for i in range(10):  # 20 s only
        a.add(i * 2.0, -30.0)
    a.add(20.0, -5.0)
    assert a.check() is None


@pytest.fixture(scope="module")
def tone_ts(tmp_path_factory):
    out = tmp_path_factory.mktemp("tone") / "tone.ts"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc=size=160x90:rate=15:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "mpegts", str(out)])
    return out


def test_segment_loudness(tone_ts, tmp_path):
    v = segment_loudness(tone_ts)
    assert v is not None and -30 < v < -15
    bad = tmp_path / "bad.ts"
    bad.write_bytes(b"not video")
    assert segment_loudness(bad) is None
    assert segment_loudness(tmp_path / "missing.ts") is None


def test_segment_loudness_is_light(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, cwd=None, cancel=None, on_line=None, low_priority=None):
        seen["args"], seen["low"] = args, low_priority
        return "t: 0.1 M: -40.0 S: -120.7\nt: 0.2 M: -18.5 S: -20.0\nt: 0.3 M:-120.7 S: -20.0\n"

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    f = tmp_path / "x.ts"
    f.write_bytes(b"x")
    assert segment_loudness(f) == -18.5
    assert seen["low"] is True and "-vn" in seen["args"] and "ebur128" in seen["args"]


def test_meter_feeds_the_audio_signal_off_the_capture_thread(monkeypatch, tmp_path):
    threads = []

    def fake_loudness(path):
        threads.append(threading.current_thread().name)
        return -25.0

    monkeypatch.setattr(signals, "segment_loudness", fake_loudness)
    a = AudioSignal()
    m = LoudnessMeter(a)
    m.start()
    try:
        m.put(tmp_path / "1.ts", 100.0, 2.0)
        m.put(tmp_path / "2.ts", 102.0, 2.0)
        assert wait_for(lambda: len(a.samples()) == 2)
        assert a.samples() == [(100.0, -25.0), (102.0, -25.0)]
        assert threads and all(name == m.name for name in threads)
    finally:
        m.stop()
        m.join(2)
    assert not m.is_alive()


def test_big_chat_laughing_much_more_than_usual():
    c = ChatSignal()
    for b in range(12):  # a big chat: 40 messages a bin, 4 of them laughing
        for i in range(40):
            c.add(b * 10 + 1 + i * 0.2, "jaja" if i < 4 else "gol")
    assert c.check(120.0) is None
    for i in range(60):  # a bit busier (1.5x) and laughing 4.5x as much, but only 30 % of the messages
        c.add(121 + i * 0.1, "JAJAJA" if i < 18 else "gol")
    sp = c.check(130.0)
    assert sp is not None and sp.reason == "laughs"


def test_calibrated_values():
    assert (signals.BIN_S, signals.BASELINE_S, signals.MIN_HISTORY_S, signals.COOLDOWN_S) == (10.0, 300.0, 60.0, 45.0)
    assert (signals.CHAT_RATE_X, signals.CHAT_MIN_MSGS, signals.LOUD_LU) == (2.0, 10, 10.0)
    assert (signals.LAUGH_SHARE, signals.LAUGH_MIN, signals.LAUGH_RATE_X) == (0.35, 5, 4.0)
