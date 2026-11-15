import threading
import time

import pytest

from baitd import ffmpeg
from baitd.live.buffer import NoFootage, RingBuffer


@pytest.fixture
def segments(tmp_path):
    """Four 2 s MPEG-TS segments (8 s of video plus a tone), as raw bytes."""
    src = tmp_path / "src"
    src.mkdir()
    ffmpeg.run([
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest",
        # 1.9 s: a hair under 2 s so each 60-frame GOP closes a segment
        "-f", "segment", "-segment_time", "1.9", "-segment_format", "mpegts",
        str(src / "s%d.ts"),
    ])
    files = sorted(src.glob("s*.ts"), key=lambda p: int(p.stem[1:]))
    assert len(files) == 4
    return [f.read_bytes() for f in files]


def _fill(buf, segments, skip=()):
    for i, data in enumerate(segments):
        if i in skip:
            continue
        buf.add(i, 1000 + 2 * i, 2.0, data)


def test_cut_inside_the_buffer(tmp_path, segments):
    buf = RingBuffer(tmp_path / "ring")
    _fill(buf, segments)
    out = tmp_path / "out.mp4"
    cut = buf.cut(1003, 1006, out)
    assert abs(ffmpeg.probe(out)["duration"] - 8) < 0.2
    assert abs(cut.start - 3.0) < 0.1
    assert abs(cut.end - 6.0) < 0.1
    assert not cut.gap
    assert cut.path == out


def test_cut_across_a_gap(tmp_path, segments):
    buf = RingBuffer(tmp_path / "ring")
    _fill(buf, segments, skip={2})
    out = tmp_path / "out.mp4"
    cut = buf.cut(1001, 1007, out)
    assert cut.gap
    assert abs(ffmpeg.probe(out)["duration"] - 6) < 0.2
    assert abs(cut.end - 5.0) < 0.1


def test_prune_and_pin(tmp_path, segments):
    folder = tmp_path / "ring"
    buf = RingBuffer(folder, keep_s=4)
    _fill(buf, segments)
    assert buf.seconds() <= 6
    assert not (folder / f"{0:010d}.ts").exists()
    assert buf.tail() == 1002
    assert buf.head() == 1008
    buf.pin(1001)
    buf.add(4, 1008, 2.0, segments[0])
    assert (folder / f"{1:010d}.ts").exists()
    assert buf.tail() == 1002


def test_cut_outside_raises(tmp_path, segments):
    buf = RingBuffer(tmp_path / "ring")
    _fill(buf, segments)
    with pytest.raises(NoFootage):
        buf.cut(2000, 2010, tmp_path / "out.mp4")


def test_clear_empties_the_buffer(tmp_path, segments):
    folder = tmp_path / "ring"
    buf = RingBuffer(folder)
    _fill(buf, segments)
    buf.clear()
    assert buf.head() is None and buf.tail() is None and buf.seconds() == 0
    assert not list(folder.glob("*.ts"))


def test_add_does_not_wait_for_a_running_cut(tmp_path, segments, monkeypatch):
    folder = tmp_path / "ring"
    buf = RingBuffer(folder, keep_s=4)
    _fill(buf, segments[:3])
    started, release = threading.Event(), threading.Event()

    def blocked_run(args, *a, **kw):
        started.set()
        assert release.wait(10)
        return ""

    monkeypatch.setattr(ffmpeg, "run", blocked_run)
    result = {}

    def do_cut():
        result["cut"] = buf.cut(1000, 1006, tmp_path / "out.mp4")

    t = threading.Thread(target=do_cut)
    t.start()
    assert started.wait(5)
    t0 = time.monotonic()
    buf.add(3, 1006, 2.0, segments[3])
    buf.add(4, 1008, 2.0, segments[0])
    buf.head(), buf.tail(), buf.seconds()
    assert time.monotonic() - t0 < 1.0
    # segments being cut are still on disk although they fell out of the window
    assert (folder / f"{0:010d}.ts").exists()
    release.set()
    t.join(10)
    assert not t.is_alive() and "cut" in result
    assert not (folder / f"{0:010d}.ts").exists()
    assert not (folder / f"{1:010d}.ts").exists()
    assert buf.tail() == 1004


def test_cut_maps_file_time_back_to_the_buffer_clock(tmp_path, segments):
    buf = RingBuffer(tmp_path / "ring")
    _fill(buf, segments, skip={1})  # footage 1000-1002 and 1004-1008
    cut = buf.cut(1001, 1007, tmp_path / "out.mp4", pad=0)
    assert cut.spans == ((1000.0, 2.0), (1004.0, 2.0), (1006.0, 2.0))
    assert cut.buffer_time(cut.start) == pytest.approx(1001.0)
    assert cut.buffer_time(1.5) == pytest.approx(1001.5)
    assert cut.buffer_time(2.5) == pytest.approx(1004.5)  # after the gap, not 1002.5
    assert cut.buffer_time(5.0) == pytest.approx(1007.0)
    assert cut.buffer_time(99.0) == pytest.approx(1008.0)  # past the end: the last footage


def test_cut_starting_after_the_window(tmp_path, segments):
    buf = RingBuffer(tmp_path / "ring")
    _fill(buf, segments, skip={0})  # footage starts at 1002
    cut = buf.cut(1000, 1006, tmp_path / "out.mp4", pad=0)
    assert cut.start == 0.0 and cut.buffer_time(0.0) == pytest.approx(1002.0)


@pytest.fixture
def fmp4_segments(tmp_path):
    """Four ~2 s fMP4 fragments of one 8 s stream, each as init + fragment bytes."""
    src = tmp_path / "fsrc"
    src.mkdir()
    ffmpeg.run([
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest",
        "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", str(src / "init.mp4"), "-hls_segment_filename", str(src / "f%d.m4s"),
        str(src / "index.m3u8"),
    ])
    frags = sorted(src.glob("f*.m4s"), key=lambda p: int(p.stem[1:]))
    assert len(frags) == 4
    init = (src / "init.mp4").read_bytes()
    return [init + f.read_bytes() for f in frags]


def _has_audio(path):
    import subprocess
    err = subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, **ffmpeg.popen_kwargs()).stderr
    return "Audio:" in err and "Video:" in err


def test_cut_fmp4_segments(tmp_path, fmp4_segments):
    buf = RingBuffer(tmp_path / "ring")
    for i, data in enumerate(fmp4_segments):
        p = buf.add(i, 1000 + 2 * i, 2.0, data, ext="mp4")
        assert p == buf.folder / f"{i:010d}.mp4"
    out = tmp_path / "out.mp4"
    cut = buf.cut(1003, 1006, out)
    assert abs(ffmpeg.probe(out)["duration"] - 8) < 0.3
    assert ffmpeg.probe(out)["width"] == 320
    assert _has_audio(out)
    assert abs(cut.start - 3.0) < 0.15
    assert abs(cut.end - 6.0) < 0.15


def test_prune_and_clear_remove_mp4(tmp_path):
    buf = RingBuffer(tmp_path / "ring", keep_s=5)
    for i in range(4):
        buf.add(i, 1000 + 2 * i, 2.0, b"x", ext="mp4")
    assert not (buf.folder / f"{0:010d}.mp4").exists()
    assert (buf.folder / f"{3:010d}.mp4").exists()
    buf.clear()
    assert not list(buf.folder.glob("*"))


def test_cut_fmp4_across_a_gap_closes_it(tmp_path, fmp4_segments):
    buf = RingBuffer(tmp_path / "ring")
    for i, data in enumerate(fmp4_segments):
        if i != 2:
            buf.add(i, 1000 + 2 * i, 2.0, data, ext="mp4")
    out = tmp_path / "out.mp4"
    cut = buf.cut(1001, 1007, out)
    assert abs(ffmpeg.probe(out)["duration"] - 6) < 0.3
    assert cut.gap
    assert _has_audio(out)
    assert sorted(p.name for p in tmp_path.glob("out.*")) == ["out.mp4"]


def test_segment_loudness_reads_a_stored_fmp4_file(tmp_path, fmp4_segments):
    from baitd.live.signals import segment_loudness
    buf = RingBuffer(tmp_path / "ring")
    path = buf.add(1, 1000, 2.0, fmp4_segments[1], ext="mp4")
    level = segment_loudness(path)
    assert level is not None and level > -40
