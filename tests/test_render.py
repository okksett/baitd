import re
import subprocess
import threading
import time

import pytest

from baitd.ffmpeg import encoder_args, ffmpeg_exe, probe
from baitd.models import JobCancelled, RenderOptions, Shot, Voiceover, Word
from baitd.render import render_video

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


def _cancel_when(tmp_path, make_media, voice_6s, make_ass, predicate):
    cancel = threading.Event()
    wd = tmp_path / "wd"
    seen = []

    def on_progress(f):
        seen.append(f)
        if predicate(f):
            cancel.set()

    with pytest.raises(JobCancelled):
        _render(tmp_path, _shots6(make_media), voice_6s, make_ass, cancel=cancel,
                on_progress=on_progress, workdir=wd)
    assert not list(wd.glob("shot_*.mp4")) and not (tmp_path / "out.mp4").exists()
    return seen


def test_render_cancel_during_shots(tmp_path, make_media, voice_6s, make_ass):
    # first value > 0 arrives from a running shot encode / right after it
    seen = _cancel_when(tmp_path, make_media, voice_6s, make_ass, lambda f: f > 0)
    assert max(seen) < 0.6


def test_render_cancel_during_final_pass(tmp_path, make_media, voice_6s, make_ass):
    # progress > 0.6 only comes from the final pass's time= lines, while that ffmpeg runs
    seen = _cancel_when(tmp_path, make_media, voice_6s, make_ass, lambda f: f > 0.6)
    assert max(seen) < 1.0


def _decoded(path, sel):
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path), "-map", sel, "-f", "null", "-"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stderr
    frames = re.findall(r"frame=\s*(\d+)", err)
    t = re.findall(r"time=(\d+):(\d+):([\d.]+)", err)[-1]
    return (int(frames[-1]) if frames else 0), int(t[0]) * 3600 + int(t[1]) * 60 + float(t[2]), err


def test_render_mixed_color_sources_keep_av_in_sync(tmp_path, media_dir, make_media, make_ass):
    # bt709-tagged clip + full-range (yuvj420p) 23.976 fps clip + untagged PNG, odd voice length
    dur = 15.437
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=f=220:d={dur}", "-ar", "24000", str(media_dir / "voice_15.wav")], check=True)
    voice = Voiceover(path=media_dir / "voice_15.wav", duration=dur, words=[Word("hi", 0.1, 0.5)],
                      scene_bounds=[(0.0, dur)])
    srcs = [make_media("v_bt709.mp4", w=1280, h=720), make_media("v_full.mp4"),
            make_media("photo.png", kind="photo", w=1600, h=1200)]
    cuts = [0.0] + [round(i * dur / 15 + (0.13 if i % 2 else -0.07), 3) for i in range(1, 15)] + [dur]
    shots = [Shot(srcs[i % 3], cuts[i], cuts[i + 1]) for i in range(15)]
    out, _ = _render(tmp_path, shots, voice, make_ass)
    vframes, _, _ = _decoded(out, "0:v")
    _, atime, _ = _decoded(out, "0:a")
    info = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(out), "-f", "null", "-"],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stderr
    assert vframes == round(dur * 30)
    assert abs(atime - dur) < 0.05
    assert "Reconfiguring filter graph" not in info
    assert re.search(r"yuv420p\(tv, bt709", info), info


def test_render_falls_back_to_libx264_when_encoder_rejected(tmp_path, make_media, voice_6s, make_ass):
    opts = RenderOptions(1080, 1920, 30, "h264_does_not_exist")
    out, _ = _render(tmp_path, _shots6(make_media), voice_6s, make_ass, opts=opts)
    assert abs(probe(out)["duration"] - 6.0) < 0.15


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
