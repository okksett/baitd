import threading
from pathlib import Path

import pytest

from baitd import ffmpeg
from baitd.clip import framing
from baitd.clip.framing import CropPlan, MAX_UPSCALE, crop_filter, detect_faces, plan_crop, required_scale
from baitd.clip.models import SourceInfo
from baitd.ffmpeg import JobCancelled

CW = 606  # even-floored crop width for a 9:16 window in 1920x1080


def _src(w=1920, h=1080, path="x.mp4", duration=60.0):
    return SourceInfo(id="a" * 12, path=Path(path), title="t", channel="", url="", duration=duration,
                      width=w, height=h, fps=30.0, has_video=True)


def _box(cx, size=120, y=300, score=0.9):
    return (int(cx - size / 2), y, size, size, score)


def _samples(n, per):
    return [(i * 0.25, per(i)) for i in range(n)]


def test_required_scale_values():
    assert required_scale(1920, 1080, 1080, 1920) == pytest.approx(1.778, abs=0.01)
    assert required_scale(1280, 720, 1080, 1920) == pytest.approx(2.667, abs=0.01)
    assert required_scale(3840, 2160, 1080, 1920) == pytest.approx(0.889, abs=0.01)
    assert required_scale(1920, 1080, 1920, 1080) == pytest.approx(1.0)


def test_small_source_forces_completo(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("detection must not run")
    monkeypatch.setattr(framing, "detect_faces", boom)
    monkeypatch.setattr(framing, "scene_cuts", boom)
    plan = plan_crop(_src(1280, 720), 0, 20, 1080, 1920, "cara", None)
    assert plan.mode == "completo"
    assert (plan.crop_w, plan.crop_h) == (1280, 720)
    assert len(plan.keys) == 1 and plan.sharpen is False
    assert required_scale(1280, 720, 1080, 1920) > MAX_UPSCALE


def test_1080p_cara_sharpens_and_stays_in_frame():
    faces = _samples(40, lambda i: [_box(200 + i * 42)])  # walks from the left edge past the right one
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "cara", None, faces=faces, cuts=[])
    assert plan.mode == "cara" and plan.sharpen is True
    assert (plan.crop_w, plan.crop_h) == (CW, 1080)
    assert plan.crop_w % 2 == 0 and plan.crop_h % 2 == 0
    xs = [x for _, x in plan.keys]
    assert all(0 <= x <= 1920 - CW for x in xs)
    assert xs[-1] > xs[0] and xs[-1] > 1000  # it follows the face


def test_no_faces_falls_back_to_center():
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "cara", None, faces=_samples(40, lambda i: []), cuts=[])
    assert plan.mode == "cara"
    assert plan.keys == [(0.0, (1920 - CW) // 2)]
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "cara", None, faces=[], cuts=[])
    assert plan.keys == [(0.0, (1920 - CW) // 2)]


def test_centro_is_a_single_center_key_without_detection(monkeypatch):
    monkeypatch.setattr(framing, "detect_faces", lambda *a, **k: pytest.fail("no detection for centro"))
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "centro", None)
    assert plan.mode == "centro" and plan.keys == [(0.0, (1920 - CW) // 2)]


def test_16x9_output_from_16x9_source_is_one_centered_key():
    plan = plan_crop(_src(), 0, 10, 1920, 1080, "cara", None, faces=_samples(8, lambda i: [_box(300)]), cuts=[])
    assert (plan.crop_w, plan.crop_h) == (1920, 1080)
    assert plan.keys == [(0.0, 0)] and plan.sharpen is False


def test_jump_at_scene_cut_not_slide():
    faces = _samples(40, lambda i: [_box(300 if i * 0.25 < 5 else 1600)])
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "cara", None, faces=faces, cuts=[5.0])
    at_cut = [x for t, x in plan.keys if t == 5.0]
    assert len(at_cut) == 2 and at_cut[0] < at_cut[1]
    assert at_cut[0] < 300 and at_cut[1] > 1000
    times = [t for t, _ in plan.keys]
    assert times == sorted(times)
    # the old value stays put right up to the cut and the new one starts at it: no key sits in between
    idx = [i for i, (t, _) in enumerate(plan.keys) if t == 5.0]
    assert idx[1] == idx[0] + 1
    assert plan.keys[idx[0] - 1][1] == pytest.approx(at_cut[0], abs=20)


def test_main_face_does_not_flip_within_shot():
    def per(i):
        a = (200, 300, 200, 200, 0.9)       # steady, left
        b = (1500, 300, 220, 220, 0.9)      # bigger but only on odd samples, right
        return [a, b] if i % 2 else [a]
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "cara", None, faces=_samples(40, per), cuts=[])
    xs = [x for _, x in plan.keys]
    assert max(xs) - min(xs) < 120  # one track: never swings to the other person


def test_alternating_order_keeps_one_track():
    def per(i):
        a, b = (200, 300, 200, 200, 0.9), (1400, 300, 190, 190, 0.9)
        return [b, a] if i % 2 else [a, b]
    plan = plan_crop(_src(), 0, 10, 1080, 1920, "cara", None, faces=_samples(40, per), cuts=[])
    xs = [x for _, x in plan.keys]
    assert max(xs) - min(xs) < 120


def test_decimation_keeps_jump_pairs():
    keys = [(i * 0.01, (i * 7) % 600) for i in range(500)]
    keys.insert(250, (keys[249][0], 123))  # jump pair at t = keys[249][0]
    out = framing._decimate(keys)
    assert len(out) <= 200 and out[0] == keys[0] and out[-1] == keys[-1]
    t = keys[249][0]
    assert [x for tt, x in out if tt == t] == [keys[249][1], 123]


def _render(src, plan, tmp_path, name):
    out = tmp_path / name
    ffmpeg.run(["-i", str(src), "-vf", crop_filter(plan, 1080, 1920), "-an", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out)])
    info = ffmpeg.probe(out)
    return info["width"], info["height"]


def test_filters_render_exact_size(make_mp4, tmp_path):
    src = make_mp4(seconds=3)
    jump = CropPlan("cara", CW, 1080, [(0.0, 100), (1.0, 300), (1.0, 900), (2.0, 700)], True)
    many = CropPlan("cara", CW, 1080, [(i * 0.006, (i * 37) % 1300) for i in range(450)], True)
    plans = {
        "single": CropPlan("cara", CW, 1080, [(0.0, 400)], True),
        "jump": jump,
        "many": many,
        "centro": CropPlan("centro", CW, 1080, [(0.0, 657)], True),
        "completo": CropPlan("completo", 1920, 1080, [(0.0, 0)], False),
    }
    assert len(many.keys) > 200
    for name, plan in plans.items():
        assert _render(src, plan, tmp_path, name + ".mp4") == (1080, 1920), name
    assert "unsharp=5:5:0.5:5:5:0.0" in crop_filter(jump, 1080, 1920)
    assert "unsharp" not in crop_filter(plans["completo"], 1080, 1920)


def test_detect_faces_runs_on_synthetic_frames(make_mp4):
    path = make_mp4(seconds=3, w=1280, h=720)
    src = SourceInfo(id="b" * 12, path=path, title="t", channel="", url="", duration=3.0,
                     width=1280, height=720, fps=30.0, has_video=True)
    assert framing.MODEL_PATH.is_file()
    out = detect_faces(src, 0, 3, 4, threading.Event())
    assert isinstance(out, list) and 10 <= len(out) <= 13
    ts = [t for t, _ in out]
    assert ts == sorted(ts) and ts[0] == pytest.approx(0.0, abs=0.01)
    for _, boxes in out:
        for x, y, w, h, s in boxes:
            assert s >= 0.6 and 0 <= x < 1280 and 0 <= y < 720


def test_detect_faces_cancel_kills_ffmpeg(make_mp4):
    path = make_mp4(seconds=3, w=640, h=360)
    src = SourceInfo(id="c" * 12, path=path, title="t", channel="", url="", duration=3.0,
                     width=640, height=360, fps=30.0, has_video=True)
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        detect_faces(src, 0, 3, 4, ev)


def test_scene_cuts_finds_a_hard_cut(tmp_path):
    out = tmp_path / "cut.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=red:s=320x180:r=15:d=2", "-f", "lavfi",
                "-i", "color=c=blue:s=320x180:r=15:d=2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]",
                "-map", "[v]", "-pix_fmt", "yuv420p", str(out)])
    src = SourceInfo(id="d" * 12, path=out, title="t", channel="", url="", duration=4.0,
                     width=320, height=180, fps=15.0, has_video=True)
    cuts = framing.scene_cuts(src, 0, 4, None)
    assert len(cuts) == 1 and cuts[0] == pytest.approx(2.0, abs=0.2)
