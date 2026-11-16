import os
import subprocess
import threading
import time

import pytest

from baitd.ffmpeg import ffmpeg_exe, has_filter, pick_encoder, probe, run


def test_ffmpeg_exe_runs():
    out = subprocess.run([ffmpeg_exe(), "-version"], capture_output=True, text=True).stdout
    assert "ffmpeg version" in out


def test_has_required_filters():
    assert all(has_filter(f) for f in ["ass", "loudnorm", "sidechaincompress", "zoompan"])


def test_has_filter_false_for_unknown():
    assert not has_filter("definitely_not_a_filter")


def test_probe_generated_clip(tmp_path):
    p = tmp_path / "c.mp4"
    run(["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2", "-pix_fmt", "yuv420p", str(p)])
    info = probe(p)
    assert abs(info["duration"] - 2.0) < 0.1
    assert (info["width"], info["height"]) == (640, 360)
    assert round(info["fps"]) == 25


def test_run_raises_with_stderr():
    with pytest.raises(RuntimeError, match="No such file|does not exist"):
        run(["-i", "nope.mp4", "-f", "null", "-"])


def test_run_on_line_receives_lines():
    lines = []
    run(["-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=1", "-f", "null", "-"], on_line=lines.append)
    assert lines and any("Duration" in l or "frame=" in l or "Output" in l for l in lines)


def test_run_cancel_kills_process():
    from baitd.ffmpeg import JobCancelled

    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    t0 = time.time()
    with pytest.raises(JobCancelled):
        run(["-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=600", "-f", "null", "-"], cancel=cancel)
    assert time.time() - t0 < 10


def test_pick_encoder_returns_working():
    enc = pick_encoder()
    assert enc in {"h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox", "libx264"}
    assert pick_encoder() == enc


def test_probe_ignores_cover_art(tmp_path):
    cover, mp3 = tmp_path / "c.png", tmp_path / "s.mp3"
    run(["-f", "lavfi", "-i", "color=c=red:s=600x600", "-frames:v", "1", str(cover)])
    run(["-f", "lavfi", "-i", "sine=f=440:d=2", "-i", str(cover), "-map", "0:a", "-map", "1:v",
         "-c:a", "libmp3lame", "-c:v", "mjpeg", "-disposition:v", "attached_pic", str(mp3)])
    info = probe(mp3)
    assert (info["width"], info["height"], info["fps"]) == (None, None, None)
    assert abs(info["duration"] - 2.0) < 0.2


def test_low_priority_flag(monkeypatch):
    import os
    import sys

    seen = {}
    real_popen = subprocess.Popen

    def fake_popen(*a, **kw):
        seen.update(kw)
        return real_popen(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run(["-version"], low_priority=True)
    if sys.platform == "win32":
        assert seen["creationflags"] & 0x4000
        assert seen["creationflags"] & getattr(subprocess, "CREATE_NO_WINDOW", 0)
    seen.clear()
    run(["-version"])
    if sys.platform == "win32":
        assert not seen["creationflags"] & 0x4000
    assert "preexec_fn" not in seen


def _scope_is_low():
    import sys

    from baitd import ffmpeg
    kw = ffmpeg.popen_kwargs()
    return bool(kw["creationflags"] & 0x4000) if sys.platform == "win32" else ffmpeg.is_low()


def test_low_priority_scope_nests_and_restores():
    from baitd import ffmpeg
    assert _scope_is_low() is False
    with ffmpeg.low_priority():
        assert _scope_is_low() is True
        with ffmpeg.low_priority(False):
            assert _scope_is_low() is False
        assert _scope_is_low() is True
        with pytest.raises(ValueError):
            with ffmpeg.low_priority(False):
                raise ValueError("boom")
        assert _scope_is_low() is True  # restored after the exception
    assert _scope_is_low() is False
    with pytest.raises(ValueError):
        with ffmpeg.low_priority():
            raise ValueError("boom")
    assert _scope_is_low() is False

    other = []
    with ffmpeg.low_priority():
        t = threading.Thread(target=lambda: other.append(_scope_is_low()))
        t.start()
        t.join()
    assert other == [False]  # the scope is per thread


def test_run_follows_the_scope_unless_told(monkeypatch):
    import sys

    from baitd import ffmpeg
    seen = {}
    real_popen = subprocess.Popen

    def fake_popen(*a, **kw):
        seen.update(kw)
        return real_popen(*a, **kw)

    def low():
        return bool(seen["creationflags"] & 0x4000) if sys.platform == "win32" else bool(nice)

    nice = []
    monkeypatch.setattr(ffmpeg, "_set_nice", lambda pid: nice.append(pid))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with ffmpeg.low_priority():
        run(["-version"])
        assert low() is True
        seen.clear()
        nice.clear()
        run(["-version"], low_priority=False)  # an explicit value wins
        assert low() is False
    seen.clear()
    run(["-version"], low_priority=True)
    assert low() is True


def test_posix_low_priority_uses_setpriority_after_start_not_preexec_fn(monkeypatch):
    from baitd import ffmpeg
    calls = []
    monkeypatch.setattr(ffmpeg, "_IS_WIN", False)
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(os, "setpriority", lambda which, pid, prio: calls.append((which, prio)), raising=False)
    seen = {}
    real_popen = subprocess.Popen

    def fake_popen(*a, **kw):
        seen.update(kw)
        return real_popen(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run(["-version"], low_priority=True)
    assert "preexec_fn" not in seen
    assert calls == [(0, 10)]
    calls.clear()
    run(["-version"], low_priority=False)
    assert calls == []
    # an error from setpriority is ignored
    def boom(*a):
        raise PermissionError
    monkeypatch.setattr(os, "setpriority", boom, raising=False)
    run(["-version"], low_priority=True)
