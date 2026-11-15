import threading
import time
from pathlib import Path

import pytest

from baitd import ffmpeg
from baitd.live import channels, store
from baitd.live.buffer import REVIEW_PAD_S
from baitd.live.channels import Channel, LiveStatus
from baitd.live.session import LiveManager, StatusBoard
from baitd.models import UserError

SETTINGS = {"live_before_s": 45, "live_after_s": 15, "live_max_height": 1080}


def wait_for(cond, timeout=10.0):
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


class FakeCapture:
    """Stands in for Capture: no thread, no network; the test sets state, server_now and the buffer."""

    made: list["FakeCapture"] = []

    def __init__(self, channel, buffer, max_height, on_event, **kw):
        self.channel, self.buffer, self.max_height, self.on_event = channel, buffer, max_height, on_event
        self.state = "starting"
        self.title = "Directo de prueba"
        self.info = {"uploader": "Westcol"}
        self.has_pdt = True
        self.now = 0.0
        self.started = self.stopped = False
        FakeCapture.made.append(self)

    def start(self):
        self.started = True
        self.state = "capturing"

    def stop(self, timeout=10.0):
        self.stopped = True
        self.state = "ended"

    def server_now(self):
        return self.now

    def latency(self):
        head = self.buffer.head()
        return None if head is None else self.now - head

    def feed(self, segs, first=0, count=None, t0=1000.0):
        """Add segments `first..first+count` as 1 s segments starting at t0 + i."""
        count = len(segs) - first if count is None else count
        for i in range(first, first + count):
            self.buffer.add(i, t0 + i, 1.0, segs[i % len(segs)])


@pytest.fixture
def chans(tmp_data):
    mine = Channel(id="a" * 12, platform="kick", handle="yo", url="https://kick.com/yo", mine=True)
    other = Channel(id="b" * 12, platform="kick", handle="westcol", url="https://kick.com/westcol")
    channels.save_channels([mine, other])
    return mine, other


@pytest.fixture
def mgr(tmp_data, chans):
    FakeCapture.made = []
    m = LiveManager(tmp_data, lambda: dict(SETTINGS), capture_factory=FakeCapture, tick_s=0.05,
                    status_fn=lambda ch: LiveStatus(False))
    yield m
    m.close()


def _cap():
    return FakeCapture.made[-1]


def test_manual_mark_uses_server_time(mgr, chans):
    mgr.start(chans[0].id)
    cap = _cap()
    assert cap.started and cap.max_height == 1080
    for i in range(50):  # 954..1004, so the 45 s before the press are buffered
        cap.buffer.add(i, 954.0 + i, 1.0, b"x")
    assert cap.buffer.head() == 1004.0
    cap.now = 1006.0
    ev = mgr.mark()
    assert ev["kind"] == "marked"
    p = mgr._session.tracker.pending()
    assert (p.start, p.end) == (961.0, 1021.0)  # 1006 - 45, not 1004 - 45 = 959
    assert cap.buffer._pin == 961.0 - REVIEW_PAD_S
    st = mgr.status()
    assert st["pending"] == {"remaining_s": 17.0, "length_s": 60.0}  # 1021 - (server_now 1006 - latency 2)


def test_other_channel_mark_uses_head(mgr, chans):
    mgr.start(chans[1].id)
    cap = _cap()
    for i in range(50):
        cap.buffer.add(i, 954.0 + i, 1.0, b"x")
    cap.now = 1006.0
    mgr.mark()
    p = mgr._session.tracker.pending()
    assert (p.start, p.end) == (959.0, 1019.0)
    assert mgr.status()["pending"]["remaining_s"] == 15.0


def test_mark_clamps_to_buffer_tail_and_extends(mgr, chans):
    mgr.start(chans[1].id)
    cap = _cap()
    for i in range(10):
        cap.buffer.add(i, 1000.0 + i, 1.0, b"x")
    assert mgr.mark()["kind"] == "marked"
    assert mgr._session.tracker.pending().start == 1000.0
    ev = mgr.mark("hotkey")
    assert ev["kind"] == "extended"
    assert mgr._session.tracker.pending().end == 1010.0 + 15 + 15


def test_mark_without_capture(mgr):
    ev = mgr.mark()
    assert ev["kind"] == "no_capture"
    assert ev["text"] == "No hay ninguna captura activa → Elige un canal y pulsa Capturar"
    st = mgr.status()
    assert st["state"] == "idle" and st["pending"] is None and st["channel_id"] is None
    assert st["buffered_s"] == 0 and st["ai_left"] is None
    assert [e["kind"] for e in st["events"]] == ["no_capture"]
    assert mgr.status(since=ev["id"])["events"] == []


def test_ready_mark_is_saved(mgr, chans, segs):
    mgr.start(chans[1].id)
    cap = _cap()
    cap.feed(segs, 0, 12)  # 1000..1012
    mgr.mark()  # (967 -> 1000, 1027)
    cap.feed(segs, 12, 18)  # head 1030, past the mark's end
    assert wait_for(lambda: len(store.list_moments()) == 1)
    m = store.list_moments()[0]
    assert m["reason"] == "manual" and m["handle"] == "westcol"
    assert m["title"].startswith("Westcol · ")
    assert wait_for(lambda: "saved" in [e["kind"] for e in mgr.status()["events"]])
    assert mgr.status()["pending"] is None


def test_stream_end_seals_pending_mark(mgr, chans, segs):
    mgr.start(chans[1].id)
    cap = _cap()
    cap.feed(segs, 0, 12)  # 12 s of footage
    mgr.mark()
    cap.state = "ended"
    assert wait_for(lambda: len(store.list_moments()) == 1)
    assert mgr.status()["state"] == "ended"
    assert mgr.mark()["kind"] == "no_capture"

    mgr.start(chans[1].id)  # a new start after the stream ended is allowed
    cap = _cap()
    cap.feed(segs, 0, 5)  # 5 s of footage
    mgr.mark()
    cap.state = "ended"
    assert wait_for(lambda: "too_short" in [e["kind"] for e in mgr.status()["events"]])
    ev = [e for e in mgr.status()["events"] if e["kind"] == "too_short"][0]
    assert ev["text"] == "El momento quedó demasiado corto y no se guardó"
    assert len(store.list_moments()) == 1


def test_no_footage_event(mgr, chans, monkeypatch):
    mgr.start(chans[1].id)
    cap = _cap()
    cap.now = 1000.0
    mgr.mark()  # nothing buffered: the mark uses the server clock
    cap.state = "error"
    assert wait_for(lambda: "no_footage" in [e["kind"] for e in mgr.status()["events"]])
    assert store.list_moments() == []


def test_close_seals_and_clears_buffer(mgr, chans, segs, tmp_data):
    mgr.start(chans[1].id)
    cap = _cap()
    cap.feed(segs, 0, 14)
    mgr.mark()
    mgr.close()
    assert len(store.list_moments()) == 1
    assert cap.stopped
    root = tmp_data / "directo" / "buffer"
    assert not any(p.is_file() for p in root.rglob("*"))
    assert mgr.status()["state"] == "idle"


def test_close_has_a_deadline_and_cancels_a_stuck_cut(mgr, chans, monkeypatch):
    seen = {}

    def stuck_ffmpeg(args, cwd=None, cancel=None, on_line=None, low_priority=False):
        seen["cancel"] = cancel
        if cancel is None or not cancel.wait(30):  # FFmpeg that never finishes on its own
            raise AssertionError("the cut was never cancelled")
        seen["cancelled"] = True
        raise ffmpeg.JobCancelled()

    monkeypatch.setattr(ffmpeg, "run", stuck_ffmpeg)
    mgr.start(chans[1].id)
    cap = _cap()
    for i in range(14):
        cap.buffer.add(i, 1000.0 + i, 1.0, b"x")
    mgr.mark()
    began = time.monotonic()
    mgr.close(timeout=0.3)
    assert time.monotonic() - began < 0.3 + 2.5  # the deadline, plus the short grace after cancelling
    assert seen.get("cancelled") is True
    assert store.list_moments() == []
    assert "save_error" not in [e["kind"] for e in mgr.status()["events"]]


def test_stop_is_idle_and_seals(mgr, chans, segs):
    mgr.start(chans[1].id)
    cap = _cap()
    cap.feed(segs, 0, 14)
    mgr.mark()
    mgr.stop()
    assert mgr.status()["state"] == "idle"
    assert len(store.list_moments()) == 1


def test_start_clears_old_buffer(mgr, chans, tmp_data):
    stale = tmp_data / "directo" / "buffer" / "viejo" / "0000000001.ts"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    mgr.start(chans[1].id)
    assert not stale.exists()


def test_second_start_refused(mgr, chans):
    mgr.start(chans[0].id)
    with pytest.raises(UserError) as e:
        mgr.start(chans[1].id)
    assert str(e.value) == "Ya hay una captura en marcha → Detenla antes de empezar otra"
    assert len(FakeCapture.made) == 1


def test_unknown_channel_refused(mgr):
    with pytest.raises(UserError) as e:
        mgr.start("c" * 12)
    assert e.value.cause == "Ese canal ya no está en la lista"


def test_capture_events_are_forwarded(mgr, chans):
    mgr.start(chans[1].id)
    _cap().on_event("started", {"text": "Captura en marcha"})
    st = mgr.status()
    assert st["events"][-1]["kind"] == "started" and st["events"][-1]["text"] == "Captura en marcha"
    assert st["state"] == "capturing" and st["channel_id"] == chans[1].id
    assert st["title"] == "Directo de prueba" and st["has_pdt"] is True


def test_events_ring_keeps_30(mgr):
    for _ in range(40):
        mgr.mark()
    ids = [e["id"] for e in mgr.status()["events"]]
    assert len(ids) == 30 and ids == list(range(11, 41))


# ---- the channel-status board ----

def _warnings(caplog, text):
    return [r for r in caplog.records if r.levelname == "WARNING" and text in r.getMessage()]


def test_board_refreshes_and_logs_a_failing_channel_once(tmp_data, chans, caplog):
    calls = []
    fail = {"on": True}
    lock = threading.Lock()

    def status(ch):
        with lock:
            calls.append(ch.id)
        if ch.id == chans[1].id and fail["on"]:
            raise RuntimeError("https://x/?token=secret")
        return LiveStatus(True, title="t", viewers=3, name="Nombre")

    board = StatusBoard(status, every_s=0.05)
    try:
        board.touch()
        assert wait_for(lambda: calls.count(chans[1].id) >= 3)
        assert board.get(chans[0].id).live is True and board.get(chans[0].id).name == "Nombre"
        assert board.get(chans[1].id).live is False
        assert len(_warnings(caplog, "westcol")) == 1 and "secret" not in caplog.text
        fail["on"] = False
        assert wait_for(lambda: board.get(chans[1].id).live is True)
        fail["on"] = True
        n = calls.count(chans[1].id)
        assert wait_for(lambda: calls.count(chans[1].id) >= n + 2)
        assert len(_warnings(caplog, "westcol")) == 2  # failed again after recovering
    finally:
        board.close()


def test_board_stops_when_nobody_asks(tmp_data, chans):
    now = {"t": 0.0}
    calls = []
    board = StatusBoard(lambda ch: calls.append(ch.id) or LiveStatus(False), every_s=0.02,
                        clock=lambda: now["t"])
    try:
        board.touch()
        assert wait_for(lambda: len(calls) >= 2)
        now["t"] = 601.0  # 10 minutes without a client
        time.sleep(0.1)
        n = len(calls)
        time.sleep(0.2)
        assert len(calls) == n
        board.touch()
        assert wait_for(lambda: len(calls) > n)
    finally:
        board.close()


def test_board_runs_at_most_4_checks_at_once(tmp_data):
    many = [Channel(id=f"{i:012x}", platform="kick", handle=f"canal{i}", url=f"https://kick.com/canal{i}")
            for i in range(10)]
    channels.save_channels(many)
    running = {"now": 0, "max": 0}
    lock = threading.Lock()

    def status(ch):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        time.sleep(0.05)
        with lock:
            running["now"] -= 1
        return LiveStatus(False)

    board = StatusBoard(status, every_s=60)
    try:
        board.refresh()
        assert wait_for(lambda: all(board.get(c.id) is not None for c in many))
        assert running["max"] == 4
    finally:
        board.close()


# ---- automatic detection (Task 13) ----

class FakeReader:
    made: list["FakeReader"] = []

    def __init__(self, channel, on_message, on_event, video_id=""):
        self.channel, self.on_message, self.on_event, self.video_id = channel, on_message, on_event, video_id
        self.started = self.stopped = False
        FakeReader.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass


class FakeDetector:
    made: list["FakeDetector"] = []

    def __init__(self, buffer, channel, settings, profile, *, save, limit, clock, cancel=None, **kw):
        self.buffer, self.channel, self.profile, self.save, self.limit = buffer, channel, profile, save, limit
        self.spikes, self.heads = [], []
        self.stopped = self.joined = self.flushed = False
        self.first = None
        FakeDetector.made.append(self)

    def spike(self, s):
        self.spikes.append(s)

    def tick(self, head):
        self.heads.append(head)

    def flush(self):
        self.flushed = True

    def earliest(self):
        return self.first

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True


@pytest.fixture
def auto(tmp_data, chans):
    FakeCapture.made, FakeReader.made, FakeDetector.made = [], [], []
    now = {"t": 0.0}
    m = LiveManager(tmp_data, lambda: dict(SETTINGS, live_ai_per_hour=4), capture_factory=FakeCapture,
                    tick_s=0.05, status_fn=lambda ch: LiveStatus(False), clock=lambda: now["t"],
                    chat_factory=FakeReader, detector_factory=FakeDetector)
    yield m, now
    m.close()


def test_other_channel_runs_detection(auto, chans, monkeypatch):
    from baitd.live import signals
    mgr, now = auto
    monkeypatch.setattr(signals, "segment_loudness", lambda path: -30.0 if "quiet" in path.name else -15.0)
    mgr.start(chans[1].id)
    cap = _cap()
    cap.info = {"uploader": "Westcol", "id": "vid123"}
    det = FakeDetector.made[-1]
    assert det.channel.handle == "westcol" and det.limit.left() == 4
    assert det.profile.id  # the first stored profile (or the default one)
    assert mgr.status()["ai_left"] == 4
    for i in range(200):
        cap.buffer.add(i, 2000.0 + i, 1.0, b"x")  # head 2200
    assert wait_for(lambda: FakeReader.made and FakeReader.made[-1].started)
    reader = FakeReader.made[-1]
    assert reader.channel.handle == "westcol" and reader.video_id == "vid123"

    for b in range(12):  # one message per 10 s bin, then the chat explodes
        reader.on_message(b * 10.0 + 1, "hola")
    for i in range(12):
        reader.on_message(121.0 + i * 0.5, "QUE DIJO")
    now["t"] = 130.5
    assert wait_for(lambda: det.spikes)
    sp = det.spikes[0]
    assert sp.reason == "chat" and sp.t == pytest.approx(120.0 + 2200.0 - 130.5)
    assert det.heads and det.heads[-1] == 2200.0

    for i in range(40):  # the capture's hook only queues; the meter feeds the audio signal
        cap.on_segment(Path(f"quiet{i}.ts"), 2000.0 + i * 2, 2.0)
        if i == 20:  # the meter's queue is bounded: let it catch up as a real stream would
            assert wait_for(lambda: len(mgr._session.audio_signal.samples()) == 21)
    cap.on_segment(Path("loud.ts"), 2080.0, 2.0)
    assert wait_for(lambda: any(s.reason == "loud" for s in det.spikes))
    assert [s.t for s in det.spikes if s.reason == "loud"] == [2080.0]

    reader.on_event("chat_off", {"text": "No se pudo leer el chat de Kick → Baitd sigue solo con el audio"})
    assert "chat_off" in [e["kind"] for e in mgr.status()["events"]]

    det.first = 2100.0  # the detector's windows keep their footage
    assert wait_for(lambda: cap.buffer._pin == 2100.0 - REVIEW_PAD_S)

    mgr.stop()
    assert reader.stopped and det.stopped and det.joined


def test_detector_moments_are_named_like_marks(auto, chans, segs):
    mgr, _ = auto
    mgr.start(chans[1].id)
    cap = _cap()
    cap.feed(segs, 0, 30)
    det = FakeDetector.made[-1]
    cut = cap.buffer.cut(1005.0, 1025.0, tmp_cut := cap.buffer.folder.parent / "c.mp4")
    det.save(cut, reason="laughs", at=1015.0, title="", kind="interesante", classified=False,
             description="", hashtags=[], text="jaja")
    det.save(cap.buffer.cut(1005.0, 1025.0, tmp_cut), reason="chat", at=1015.0, title="Se cae", kind="gracioso",
             classified=True, description="Una caída.", hashtags=["#risa"], text="")
    ms = sorted(store.list_moments(), key=lambda m: m["reason"])
    assert [(m["reason"], m["kind"], m["classified"]) for m in ms] == [
        ("chat", "gracioso", True), ("laughs", "interesante", False)]
    assert ms[1]["title"].startswith("Westcol · ") and ms[0]["title"] == "Se cae"
    assert [e["text"] for e in mgr.status()["events"] if e["kind"] == "saved"] == ["Baitd encontró un momento"] * 2


def test_stream_end_flushes_the_detector(auto, chans):
    mgr, _ = auto
    mgr.start(chans[1].id)
    cap = _cap()
    det = FakeDetector.made[-1]
    cap.state = "ended"
    assert wait_for(lambda: det.flushed)


def test_own_channel_has_no_detection(auto, chans):
    mgr, _ = auto
    mgr.start(chans[0].id)
    time.sleep(0.2)
    assert FakeDetector.made == [] and FakeReader.made == []
    assert mgr.status()["ai_left"] is None
