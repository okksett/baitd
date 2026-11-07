import threading
import time

import pytest

from baitd.ffmpeg import probe
from baitd.models import JobCancelled, RenderOptions, Shot
from baitd.render import encoder_args, render_video

V = RenderOptions(1080, 1920, 30, "libx264")
H60 = RenderOptions(1920, 1080, 60, "libx264")


def _render(tmp_path, shots, voice, make_ass, opts=V, music=None, vol=0.15, cancel=None,
            on_progress=None, workdir=None):
    wd = workdir or tmp_path / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    ass = make_ass(wd / "subs.ass", opts.width, opts.height)
    out = tmp_path / "out.mp4"
    return render_video(shots, voice, ass, music, vol, opts, wd, out, cancel or threading.Event(),
                        on_progress or (lambda f: None)), wd


def _shots6(make_media):
    return [Shot(make_media("v_port.mp4"), 0.0, 2.0), Shot(make_media("v_short.mp4"), 2.0, 4.0),
            Shot(make_media("v_land4k.mp4", w=3840, h=2160), 4.0, 6.0)]


def test_render_vertical_specs(tmp_path, make_media, voice_6s, make_ass):
    t0 = time.time()
    progress = []
    out, wd = _render(tmp_path, _shots6(make_media), voice_6s, make_ass, on_progress=progress.append)
    print("vertical render seconds:", round(time.time() - t0, 2))
    i = probe(out)
    assert (i["width"], i["height"]) == (1080, 1920)
    assert round(i["fps"]) == 30 and abs(i["duration"] - 6.0) < 0.15
    assert progress[0] == 0.0 and progress[-1] == 1.0 and progress == sorted(progress)
    assert not list(wd.glob("shot_*.mp4"))


def test_render_horizontal_60fps(tmp_path, make_media, voice_6s, make_ass):
    shots = [Shot(make_media("v_land.mp4", w=1280, h=720), 0.0, 3.0),
             Shot(make_media("v_port.mp4"), 3.0, 6.0)]
    out, _ = _render(tmp_path, shots, voice_6s, make_ass, opts=H60)
    i = probe(out)
    assert (i["width"], i["height"]) == (1920, 1080)
    assert round(i["fps"]) == 60 and abs(i["duration"] - 6.0) < 0.15


def test_render_photo_shot(tmp_path, make_media, voice_6s, make_ass):
    shots = [Shot(make_media("photo.png", kind="photo", w=1600, h=1200), 0.0, 3.0),
             Shot(make_media("v_port.mp4"), 3.0, 6.0)]
    out, _ = _render(tmp_path, shots, voice_6s, make_ass)
    assert abs(probe(out)["duration"] - 6.0) < 0.15


def test_render_with_music(tmp_path, make_media, voice_6s, make_ass, music_file):
    out, _ = _render(tmp_path, _shots6(make_media), voice_6s, make_ass, music=music_file)
    assert abs(probe(out)["duration"] - 6.0) < 0.15


def test_render_cancel_kills_ffmpeg(tmp_path, make_media, voice_6s, make_ass):
    cancel = threading.Event()
    wd = tmp_path / "wd"
    calls = []

    def on_progress(f):
        calls.append(f)
        cancel.set()

    with pytest.raises(JobCancelled):
        _render(tmp_path, _shots6(make_media), voice_6s, make_ass, cancel=cancel,
                on_progress=on_progress, workdir=wd)
    assert len(calls) == 1
    assert not list(wd.glob("shot_*.mp4")) and not (tmp_path / "out.mp4").exists()


def test_render_missing_media_path(tmp_path, make_media, voice_6s, make_ass):
    m = make_media("v_port.mp4")
    m.path = None
    with pytest.raises(ValueError):
        _render(tmp_path, [Shot(m, 0.0, 6.0)], voice_6s, make_ass)


def test_render_in_path_with_spaces_and_accents(tmp_path, make_media, voice_6s, make_ass):
    wd = tmp_path / "José dir" / "¿qué ñ [1],'x'"  # "?" and ":" are illegal in Windows names
    out, _ = _render(tmp_path, _shots6(make_media), voice_6s, make_ass, workdir=wd)
    assert abs(probe(out)["duration"] - 6.0) < 0.15


def test_hw_encoder_args_valid():
    for enc in ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"):
        a = encoder_args(enc)
        assert "-pix_fmt" in a and a[a.index("-pix_fmt") + 1] == "yuv420p" and enc in a
    assert "-crf" in encoder_args("libx264")
