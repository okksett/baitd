import json
import threading
import time

import pytest

from baitd import config, ffmpeg
from baitd.clip import transcribe as clip_transcribe
from baitd.clip.models import Transcript
from baitd.live.ai import HourlyLimit
from baitd.live.buffer import RingBuffer
from baitd.live.channels import Channel
from baitd.live.buffer import BUFFER_S, REVIEW_PAD_S
from baitd.live.detector import CANDIDATE_AFTER_S, CANDIDATE_BEFORE_S, MAX_CANDIDATE_S, MAX_WAITING, Detector
from baitd.live.signals import Spike
from baitd.llm._http import QuotaExhausted
from baitd.models import Word

T0 = 1000.0


def wait_for(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="module")
def segs(tmp_path_factory):
    """Thirty 1 s MPEG-TS segments (video plus a tone), as raw bytes."""
    src = tmp_path_factory.mktemp("segs")
    ffmpeg.run([
        "-f", "lavfi", "-i", "testsrc=size=160x90:rate=15:duration=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest",
        "-f", "segment", "-segment_time", "0.95", "-segment_format", "mpegts",
        str(src / "s%d.ts"),
    ])
    files = sorted(src.glob("s*.ts"), key=lambda p: int(p.stem[1:]))
    assert len(files) >= 30
    return [f.read_bytes() for f in files[:30]]


def _reply(tipo="humor", inicio="0:10", fin="0:45"):
    return json.dumps({"tipo": tipo, "inicio": inicio, "fin": fin, "titulo": "Se cae de la silla",
                       "descripcion": "Una caída en pleno directo.", "hashtags": ["risa", "#directo"]})


def talk(wav, provider, language, settings, model, cancel, on_progress, **kw):
    """Fake transcriber: one short sentence every second of the window."""
    talk.calls.append({"provider": provider, "model": model, **kw})
    return Transcript(language="es", words=[Word(f"frase{i}.", i + 0.1, i + 0.6) for i in range(60)],
                      duration=60.0)


class Harness:
    def __init__(self, tmp_path, segs, complete, transcribe=talk, per_hour=6, seconds=300, skip=(),
                 keep_s=10_000):
        self.buffer = RingBuffer(tmp_path / "ts", keep_s=keep_s)
        for i in range(seconds):
            if i not in skip:
                self.buffer.add(i, T0 + i, 1.0, segs[i % len(segs)])
        self.saved = []
        self.prompts = []
        talk.calls = []

        def save(cut, **kw):
            self.saved.append({"start": cut.start, "end": cut.end, **kw})

        def fake_complete(prompt):
            self.prompts.append(prompt)
            return complete(prompt)

        self.limit = HourlyLimit(per_hour, clock=lambda: 0.0)
        ch = Channel(id="b" * 12, platform="kick", handle="westcol", url="https://kick.com/westcol")
        self.det = Detector(self.buffer, ch, lambda: {}, config.default_profile(), save=save,
                            transcribe=transcribe, complete=fake_complete, limit=self.limit, clock=lambda: 0.0)

    def run(self, *spikes, head=T0 + 300):
        for s in spikes:
            self.det.spike(s)
        self.det.tick(head)
        assert wait_for(self.det.idle)

    def close(self):
        self.det.stop()
        self.det.join(10)


@pytest.fixture
def harness(tmp_path, segs):
    made = []

    def make(complete, **kw):
        h = Harness(tmp_path, segs, complete, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def test_candidate_window():
    assert (CANDIDATE_BEFORE_S, CANDIDATE_AFTER_S) == (40, 20)


def test_detector_merges_and_classifies(harness):
    h = harness(lambda p: _reply())
    h.run(Spike(T0 + 100, "chat", 4.0), Spike(T0 + 110, "loud", 9.0))
    assert len(h.prompts) == 1  # one candidate [1060, 1130]
    assert "[00:00] frase0." in h.prompts[0]
    assert talk.calls == [{"provider": "local", "model": "small", "cpu_threads": 2}]
    assert len(h.saved) == 1
    s = h.saved[0]
    assert (s["kind"], s["classified"], s["reason"]) == ("gracioso", True, "chat")
    assert s["title"] == "Se cae de la silla" and s["hashtags"] == ["#risa", "#directo"]
    assert s["description"] == "Una caída en pleno directo."
    # the AI's span 0:10-0:45 of the window is [1070, 1105] plus the rest of the line marked 0:45 (it ends at
    # 1105.6); the saved file starts 20 s of padding earlier
    assert (s["start"], s["end"]) == pytest.approx((20.0, 55.6))
    assert s["text"].startswith("frase10.") and s["text"].endswith("frase45.")
    assert s["at"] == T0 + 100


def test_detector_waits_for_the_window_to_end(harness):
    h = harness(lambda p: _reply())
    h.det.spike(Spike(T0 + 290, "chat", 4.0))
    h.det.tick(T0 + 300)  # the window ends at 1310: not yet
    time.sleep(0.2)
    assert h.prompts == [] and h.saved == []
    assert h.det.earliest() == T0 + 250


def test_detector_drops_nada(harness):
    h = harness(lambda p: _reply(tipo="nada"))
    h.run(Spike(T0 + 100, "laughs", 0.7))
    assert len(h.prompts) == 1
    assert h.saved == []


def test_detector_quota_saves_unclassified(harness):
    def out_of_quota(prompt):
        raise QuotaExhausted("Se acabó la cuota gratis de la IA por hoy", "Vuelve mañana")

    h = harness(out_of_quota)
    h.run(Spike(T0 + 80, "chat", 4.0), Spike(T0 + 200, "laughs", 0.6))
    assert len(h.prompts) == 1  # the second candidate makes no AI call
    assert [(s["classified"], s["kind"], s["reason"]) for s in h.saved] == [
        (False, "interesante", "chat"), (False, "interesante", "laughs")]
    assert h.saved[0]["title"] == ""  # the manager names it like a manual mark
    assert (h.saved[0]["start"], h.saved[0]["end"]) == pytest.approx((20.0, 80.0))
    assert h.limit.left() == 0


def test_detector_respects_hourly_limit(harness):
    h = harness(lambda p: _reply(), per_hour=1)
    h.run(Spike(T0 + 80, "chat", 4.0), Spike(T0 + 160, "chat", 4.0), Spike(T0 + 240, "loud", 9.0))
    assert len(h.prompts) == 1
    assert [s["classified"] for s in h.saved] == [True, False, False]
    assert [s["reason"] for s in h.saved] == ["chat", "chat", "loud"]


def test_detector_without_words_saves_unclassified(harness):
    def silent(*a, **kw):
        raise clip_transcribe.no_speech()

    h = harness(lambda p: _reply(), transcribe=silent)
    h.run(Spike(T0 + 100, "loud", 9.0))
    assert h.prompts == []
    assert [(s["classified"], s["reason"]) for s in h.saved] == [(False, "loud")]


def test_unreadable_reply_saves_unclassified(harness):
    h = harness(lambda p: "no sé")
    h.run(Spike(T0 + 100, "chat", 4.0))
    assert len(h.prompts) == 1
    assert [s["classified"] for s in h.saved] == [False]


def test_stop_saves_what_is_left_without_ai(harness):
    h = harness(lambda p: _reply())
    h.det.spike(Spike(T0 + 270, "chat", 4.0))  # window [1230, 1290]: not ended at head 1285
    h.det.tick(T0 + 285)
    h.det.stop()
    h.det.join(10)
    assert not h.det.is_alive()
    assert h.prompts == []
    assert [(s["classified"], s["reason"]) for s in h.saved] == [(False, "chat")]
    assert (h.saved[0]["start"], h.saved[0]["end"]) == pytest.approx((20.0, 75.0))  # clamped to the head
    h.det.spike(Spike(T0 + 100, "chat", 4.0))  # ignored once stopped
    assert h.det.idle()


def test_words_follow_the_footage_across_a_gap(harness):
    # an ad dropped 1070-1089: the window [1060, 1120] is 40 s of file, and file 15.1 is 1095.1 on the stream
    h = harness(lambda p: _reply(inicio="0:35", fin="0:45"), skip=range(70, 90))
    h.run(Spike(T0 + 100, "chat", 4.0))
    assert "[00:35] frase15." in h.prompts[0] and "[00:15]" not in h.prompts[0]
    s = h.saved[0]
    assert s["text"].startswith("frase15.") and s["text"].endswith("frase25.")


def test_a_stuck_candidate_does_not_hold_the_buffer(harness, segs):
    running = threading.Event()

    def stuck(wav, provider, language, settings, model, cancel, on_progress, **kw):
        running.set()
        cancel.wait()  # a model download or an endless request; only stop() ends it
        raise ffmpeg.JobCancelled()

    h = harness(lambda p: _reply(), transcribe=stuck, keep_s=BUFFER_S)
    h.det.spike(Spike(T0 + 100, "chat", 4.0))
    h.det.tick(T0 + 300)
    assert running.wait(10)
    for k in range(5):  # five more windows while it is stuck: only the newest MAX_WAITING wait
        h.det.spike(Spike(T0 + 400 + 80 * k, "chat", 4.0))
    for i in range(300, 800):
        h.buffer.add(i, T0 + i, 1.0, segs[i % len(segs)])
    head = T0 + 800
    h.det.tick(head)
    assert [c.start for c in h.det._waiting] == [T0 + 520, T0 + 600, T0 + 680][-MAX_WAITING:]
    floor = head - BUFFER_S - MAX_CANDIDATE_S
    assert h.det.earliest() == floor  # not 1060, the stuck window's start
    h.buffer.pin(h.det.earliest() - REVIEW_PAD_S)
    h.buffer.add(800, head, 1.0, segs[0])  # pruning runs on add
    assert h.buffer.tail() >= floor - REVIEW_PAD_S - 1
