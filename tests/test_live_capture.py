from datetime import datetime, timezone

import pytest

from baitd.live import capture as cap_mod
from baitd.live.buffer import RingBuffer
from baitd.live.capture import Capture
from baitd.live.channels import DEFAULT_DELAY_S, Channel, Offline

PL = "https://cdn.test/live/index.m3u8"
T0 = 1_800_000_000.0


class Resp:
    def __init__(self, status=200, text="", content=b"", headers=None):
        self.status_code = status
        self.text = text
        self.content = content
        self.headers = headers or {}


class FakeHttp:
    """get(url) answers queued items per url; an Exception item is raised."""

    def __init__(self):
        self.playlists: list = []
        self.requested: list[str] = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        if url.endswith(".m3u8"):
            item = self.playlists.pop(0)
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, Resp) else Resp(text=item)
        return Resp(content=b"ts-" + url.rsplit("/", 1)[1].encode())

    @property
    def segment_urls(self):
        return [u for u in self.requested if u.endswith(".ts")]


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def playlist(first, count, *, dur=2.0, pdt0=None, titles=None, extra="", ended=False, prefetch=False, target=2):
    lines = ["#EXTM3U", f"#EXT-X-TARGETDURATION:{target}", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
    lines.append(extra) if extra else None
    for i in range(count):
        seq = first + i
        if pdt0 is not None:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso(pdt0 + i * dur)}")
        title = (titles or {}).get(seq, "live")
        lines.append(f"#EXTINF:{dur},{title}")
        lines.append(f"seg{seq}.ts")
    if prefetch:
        lines.append("#EXT-X-PREFETCH:https://cdn.test/live/seg999.ts")
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


class Env:
    def __init__(self, tmp_path, platform="kick", delay=None, resolve=None):
        self.now = T0
        self.http = FakeHttp()
        self.events: list[tuple[str, str]] = []
        self.resolved = 0
        self.waits: list[float] = []
        self.buf = RingBuffer(tmp_path / "buf")
        self.ch = Channel("c1", platform, "someone", f"https://{platform}.com/someone", delay_s=delay)

        def default_resolve(ch, h):
            self.resolved += 1
            return PL, {"id": "x", "uploader": "someone"}

        def wait(s):
            self.waits.append(s)
            self.now += s
            return not self.http.playlists and self.cap.state != "reconnecting"  # stop once all were served

        self.cap = Capture(self.ch, self.buf, 1080, lambda k, d: self.events.append((k, d["text"])),
                           resolve=resolve or default_resolve, http=self.http, clock=lambda: self.now, wait=wait)

    def run(self):
        self.cap.start()
        self.cap.join(10)
        assert not self.cap.is_alive()

    @property
    def kinds(self):
        return [k for k, _ in self.events]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_downloads_each_segment_once(env):
    env.http.playlists += [playlist(10, 3, pdt0=T0), playlist(11, 3, pdt0=T0 + 2), playlist(12, 3, pdt0=T0 + 4)]
    env.run()
    assert [u.rsplit("/", 1)[1] for u in env.http.segment_urls] == [f"seg{n}.ts" for n in range(10, 15)]
    assert env.buf.seconds() == pytest.approx(10.0)
    assert env.cap.state == "ended"  # stopped by the fake wait
    assert env.cap.has_pdt is True
    assert env.kinds == ["started"]


def test_on_segment_hook_gets_file_start_duration(env):
    got = []
    env.cap.on_segment = lambda p, s, d: got.append((p, s, d))
    env.http.playlists += [playlist(5, 1, pdt0=T0)]
    env.run()
    assert got == [(env.buf.folder / "0000000005.ts", T0, 2.0)]
    assert got[0][0].read_bytes() == b"ts-seg5.ts"


def test_first_poll_takes_last_60_s(env):
    env.http.playlists += [playlist(1000, 720, dur=5.0, pdt0=T0, target=5)]
    env.run()
    nums = [int(u.rsplit("seg", 1)[1][:-3]) for u in env.http.segment_urls]
    assert len(nums) == 12
    assert nums == list(range(1708, 1720))


def test_overlapping_big_windows_add_only_new_segments(env):
    env.http.playlists += [playlist(1000, 720, dur=5.0, pdt0=T0, target=5),
                           playlist(1001, 720, dur=5.0, pdt0=T0 + 5, target=5)]
    env.run()
    nums = [int(u.rsplit("seg", 1)[1][:-3]) for u in env.http.segment_urls]
    assert nums == list(range(1708, 1721))  # 12 on the first poll, exactly 1 on the second


def test_reconnect_with_overlapping_playlist_adds_only_new(env):
    env.http.playlists += [playlist(1000, 720, dur=5.0, pdt0=T0, target=5), Resp(status=500),
                           playlist(1002, 720, dur=5.0, pdt0=T0 + 10, target=5)]
    env.run()
    nums = [int(u.rsplit("seg", 1)[1][:-3]) for u in env.http.segment_urls]
    assert nums == list(range(1708, 1722))


def test_no_pdt_old_segments_are_not_chained_after_head(env):
    env.http.playlists += [playlist(1000, 720, dur=5.0, target=5), playlist(1001, 720, dur=5.0, target=5)]
    env.run()
    segs = sorted(env.buf._segs, key=lambda s: s.seq)
    assert [s.seq for s in segs][0] == 1708
    for a, b in zip(segs, segs[1:]):
        assert b.start == pytest.approx(a.end)


def test_sequence_drop_trims_again(env):
    env.http.playlists += [playlist(5000, 3, pdt0=T0), playlist(1, 100, dur=2.0, pdt0=T0 + 10)]
    env.run()
    nums = [int(u.rsplit("seg", 1)[1][:-3]) for u in env.http.segment_urls]
    assert nums[:3] == [5000, 5001, 5002]
    assert nums[3:] == list(range(71, 101))  # newest 60 s only


def test_stop_mid_batch_stops_downloading(env):
    added = []
    def hook(p, s, d):
        added.append(p)
        if len(added) == 3:
            env.cap.stop(5)
    env.cap.on_segment = hook
    env.http.playlists += [playlist(1, 10, pdt0=T0)]
    env.run()
    assert len(added) == 3
    assert len(env.http.segment_urls) == 3
    assert env.buf.seconds() == pytest.approx(6.0)


def test_ads_never_enter_the_buffer(env):
    env.http.playlists += [playlist(1, 5, pdt0=T0, titles={2: "Amazon|123", 3: "Amazon|123"})]
    env.run()
    assert [u.rsplit("/", 1)[1] for u in env.http.segment_urls] == ["seg1.ts", "seg4.ts", "seg5.ts"]
    assert env.buf.seconds() == pytest.approx(6.0)


def test_prefetch_ignored(env):
    env.http.playlists += [playlist(1, 2, pdt0=T0, prefetch=True)]
    env.run()
    assert "https://cdn.test/live/seg999.ts" not in env.http.requested
    assert len(env.http.segment_urls) == 2


def test_offset_from_server_time(env):
    extra = f'#EXT-X-DATERANGE:ID="x",START-DATE="{iso(T0)}",X-SERVER-TIME="{T0 + 4.5}"'
    env.http.playlists += [playlist(1, 2, pdt0=T0, extra=extra)]
    env.run()
    assert env.cap.offset() == pytest.approx(4.5)
    assert env.cap.server_now() == env.now + env.cap.offset()
    assert env.cap.latency() == pytest.approx(env.cap.server_now() - env.buf.head())


def test_offset_median_of_last_5(env):
    for i, off in enumerate([100.0, 4.0, 5.0, 4.5, 5.5, 4.0]):
        extra = f'#EXT-X-DATERANGE:ID="x",START-DATE="{iso(T0)}",X-SERVER-TIME="{T0 + i * 1.0 + off}"'
        env.http.playlists.append(playlist(1 + i, 1, pdt0=T0 + i * 2, extra=extra))
    env.run()
    assert env.cap.offset() == pytest.approx(4.5)  # the 100 s outlier fell out of the window


def test_offset_falls_back_to_date_header(env):
    date = "Sun, 15 Jan 2027 08:00:08 GMT"
    expected = datetime(2027, 1, 15, 8, 0, 8, tzinfo=timezone.utc).timestamp()
    env.now = expected - 3.0
    env.http.playlists += [Resp(text=playlist(1, 1, pdt0=T0), headers={"Date": date})]
    env.run()
    assert env.cap.offset() == pytest.approx(3.0)


def test_offset_defaults_to_zero(env):
    env.http.playlists += [playlist(1, 1, pdt0=T0)]
    env.run()
    assert env.cap.offset() == 0.0


def test_reconnect_then_end(tmp_path):
    answers = [(PL, {}), Offline()]

    def resolve(ch, h):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    e = Env(tmp_path, resolve=resolve)
    e.http.playlists += [Resp(status=403)]
    e.run()
    assert e.cap.state == "ended"
    assert e.kinds == ["reconnecting", "ended"]
    assert e.waits[0] == cap_mod.RESOLVE_BACKOFF_S[0]


def test_reconnect_recovers_and_keeps_buffer(env):
    env.http.playlists += [playlist(1, 2, pdt0=T0), Resp(status=500), playlist(2, 3, pdt0=T0 + 2)]
    env.run()
    assert env.resolved == 2
    assert "reconnecting" in env.kinds and "ended" not in env.kinds
    assert len(env.http.segment_urls) == 4  # seg1..seg4, each once
    assert env.cap.state == "ended"  # stopped by the fake wait, not an error


def test_three_failed_resolves_end_in_error(tmp_path):
    n = []

    def resolve(ch, h):
        n.append(1)
        raise OSError("boom")

    e = Env(tmp_path, resolve=resolve)
    e.http.playlists += [Resp(status=403)]
    # the first resolve is the one before the loop, so it must work: make it succeed once
    first = {"done": False}

    def resolve2(ch, h):
        if not first["done"]:
            first["done"] = True
            return PL, {}
        return resolve(ch, h)

    e.cap._resolve = resolve2
    e.run()
    assert e.cap.state == "error"
    assert e.kinds == ["reconnecting", "error"]
    assert len(n) == 3
    assert e.waits[:3] == list(cap_mod.RESOLVE_BACKOFF_S)


def test_new_playlist_failing_counts_as_failure(env):
    env.http.playlists += [Resp(status=403)] + [Resp(status=403)] * 3
    env.run()
    assert env.cap.state == "error"
    assert env.resolved == 4  # first resolve + three reconnect attempts


def test_segment_error_reconnects_and_retries(env):
    env.http.playlists += [playlist(1, 1, pdt0=T0), playlist(1, 1, pdt0=T0)]
    real_get = env.http.get
    calls = {"n": 0}

    def flaky(url, timeout=None):
        if url.endswith("seg1.ts"):
            calls["n"] += 1
            if calls["n"] == 1:
                env.http.requested.append(url)
                return Resp(status=404)
        return real_get(url, timeout)

    env.http.get = flaky
    env.run()
    assert calls["n"] == 2
    assert env.buf.seconds() == pytest.approx(2.0)


def test_sequence_drop_resets_seen(env):
    env.http.playlists += [playlist(50, 2, pdt0=T0), playlist(3, 2, pdt0=T0 + 4)]
    env.run()
    assert [u.rsplit("/", 1)[1] for u in env.http.segment_urls] == ["seg50.ts", "seg51.ts", "seg3.ts", "seg4.ts"]


def test_first_resolve_user_error_ends_in_error(tmp_path):
    from baitd.models import UserError

    def resolve(ch, h):
        raise UserError("No se pudo abrir el directo", "Comprueba tu conexión")

    e = Env(tmp_path, resolve=resolve)
    e.run()
    assert e.cap.state == "error"
    assert e.kinds == ["error"]
    assert "No se pudo abrir el directo" in e.events[0][1]


def test_first_resolve_offline_ends(tmp_path):
    def resolve(ch, h):
        raise Offline()

    e = Env(tmp_path, resolve=resolve)
    e.run()
    assert e.cap.state == "ended"
    assert e.kinds == ["ended"]


def test_endlist_ends(env):
    env.http.playlists += [playlist(1, 2, pdt0=T0, ended=True)]
    env.run()
    assert env.cap.state == "ended"
    assert env.kinds == ["started", "ended"]
    assert len(env.http.segment_urls) == 2


def _fmp4_playlist(entries, *, ended=False):
    """entries: ("map", uri) or (seq, "name.m4s"); 2 s segments with program-date-times."""
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]
    n = 0
    for kind, val in entries:
        if kind == "map":
            lines.append(f'#EXT-X-MAP:URI="{val}"')
        else:
            if n == 0:
                lines.append(f"#EXT-X-MEDIA-SEQUENCE:{kind}")
            lines += [f"#EXT-X-PROGRAM-DATE-TIME:{iso(T0 + 2 * n)}", "#EXTINF:2.0,live", val]
            n += 1
    if ended:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def test_fmp4_stream_is_captured(env):
    env.http.playlists += [
        _fmp4_playlist([("map", "init0.mp4"), (1, "a1.m4s"), (2, "a2.m4s"),
                        ("map", "init1.mp4"), (3, "a3.m4s")], ended=True),
    ]
    env.run()
    assert env.cap.state == "ended"
    assert "unsupported" not in env.kinds
    inits = [u.rsplit("/", 1)[1] for u in env.http.requested if u.endswith(("init0.mp4", "init1.mp4"))]
    assert inits == ["init0.mp4", "init1.mp4"]  # each init fetched once
    files = sorted(env.buf.folder.iterdir())
    assert [f.suffix for f in files] == [".mp4"] * 3
    assert [f.read_bytes() for f in files] == [
        b"ts-init0.mp4ts-a1.m4s", b"ts-init0.mp4ts-a2.m4s", b"ts-init1.mp4ts-a3.m4s",
    ]


def test_fmp4_init_cached_across_polls_and_failure_reconnects(env):
    env.http.playlists += [
        _fmp4_playlist([("map", "init0.mp4"), (1, "a1.m4s")]),
        _fmp4_playlist([("map", "init0.mp4"), (1, "a1.m4s"), (2, "a2.m4s")], ended=True),
    ]
    env.run()
    assert [u for u in env.http.requested if u.endswith("init0.mp4")] == ["https://cdn.test/live/init0.mp4"]
    assert env.cap.state == "ended"


def test_fmp4_init_failure_counts_as_network_failure(env):
    real_get = env.http.get
    calls = {"n": 0}

    def get(url, timeout=None):
        if url.endswith("init0.mp4"):
            calls["n"] += 1
            if calls["n"] == 1:
                return Resp(status=500)
        return real_get(url, timeout)

    env.http.get = get
    pl = _fmp4_playlist([("map", "init0.mp4"), (1, "a1.m4s")], ended=True)
    env.http.playlists += [pl, pl]
    env.run()
    assert "reconnecting" in env.kinds
    assert env.cap.state == "ended"
    assert [f.suffix for f in env.buf.folder.iterdir()] == [".mp4"]


def test_disk_full_stops_capture(env, monkeypatch):
    class U:
        free = 1024

    monkeypatch.setattr(cap_mod.shutil, "disk_usage", lambda p: U)
    env.http.playlists += [playlist(1, 2, pdt0=T0)]
    env.run()
    assert env.kinds == ["started", "disk_full"]
    assert env.cap.state == "error"
    assert env.http.segment_urls == []


def test_synthetic_times_without_pdt(tmp_path):
    e = Env(tmp_path, platform="kick")
    e.http.playlists += [playlist(7, 3), playlist(8, 4)]
    e.run()
    assert e.cap.has_pdt is False
    segs = {s.seq: s for s in e.buf._segs}
    # newest segment of the first poll ends `delay` behind the server clock (offset 0 here)
    assert segs[9].end == pytest.approx(T0 - DEFAULT_DELAY_S["kick"])
    # chained by sequence: contiguous
    for n in (7, 8, 9, 10):
        assert segs[n].start == pytest.approx(segs[7].start + (n - 7) * 2.0)


def test_synthetic_times_use_channel_delay(tmp_path):
    e = Env(tmp_path, platform="twitch", delay=12.0)
    e.http.playlists += [playlist(1, 1)]
    e.run()
    (seg,) = e.buf._segs
    assert seg.start == pytest.approx(T0 - 12.0 - 2.0)


def test_stop_ends_thread(tmp_path):
    import threading

    buf = RingBuffer(tmp_path / "b")
    ch = Channel("c", "kick", "x", "https://kick.com/x")
    http = FakeHttp()
    http.playlists += [playlist(1, 1, pdt0=T0)] * 50
    cap = Capture(ch, buf, 1080, lambda k, d: None, resolve=lambda c, h: (PL, {}), http=http)
    cap.start()
    cap.stop(5)
    assert not cap.is_alive()
    assert cap.state == "ended"
