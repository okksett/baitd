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
        "-f", "segment", "-segment_time", "1.9",  # a hair under 2 s so each 60-frame GOP closes a segment "-segment_format", "mpegts",
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
