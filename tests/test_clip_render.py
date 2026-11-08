import re
import subprocess
import threading
from pathlib import Path

import pytest

from baitd import captions, ffmpeg
from baitd.clip.framing import CropPlan
from baitd.clip.models import Moment, SourceInfo
from baitd.clip.render import clip_fps, keep_segments, remap_words, render_clip
from baitd.ffmpeg import JobCancelled
from baitd.config import default_profile
from baitd.models import RenderOptions, Word

OPTS = RenderOptions(1080, 1920, 30, "libx264")


def _w(text, s, e):
    return Word(text, s, e)


def _profile(**clip):
    p = default_profile()
    for k, v in clip.items():
        setattr(p.clipping, k, v)
    p.subtitle = captions.PRESETS["amarillo_clasico"]
    return p


def _src(path, w=320, h=180, fps=15.0, has_video=True, channel="Canal"):
    return SourceInfo(id="a" * 12, path=Path(path), title="t", channel=channel, url="", duration=6.0,
                      width=w, height=h, fps=fps, has_video=has_video)


def _moment(start=0.0, end=6.0, **kw):
    return Moment(start=start, end=end, kind="interesante", score=80, title="Un titulo", description="",
                  hashtags=[], text="", **kw)


def test_keep_segments_cuts_long_gaps_only():
    words = [_w("a", 1.0, 1.4), _w("b", 1.5, 1.9), _w("c", 3.0, 3.4)]  # gap 1.9 -> 3.0 is long
    segs = keep_segments(words, 0.0, 5.0)
    assert segs == [pytest.approx((0.85, 2.05)), pytest.approx((2.85, 3.55))]
    # short gaps only: one segment, trimmed to 0.15 s of silence on both ends
    assert keep_segments(words[:2], 0.0, 5.0) == [pytest.approx((0.85, 2.05))]
    # trimming never leaves the span
    assert keep_segments(words[:2], 0.95, 1.95) == [pytest.approx((0.95, 1.95))]
    # no words: the whole span
    assert keep_segments([], 2.0, 9.0) == [(2.0, 9.0)]


def test_remap_words_monotonic_and_shifted():
    words = [_w("a", 1.0, 1.4), _w("b", 1.5, 1.9), _w("c", 3.0, 3.4)]
    segs = keep_segments(words, 0.0, 5.0)
    out = remap_words(words, segs)
    assert [w.text for w in out] == ["a", "b", "c"]
    assert out[0].start == pytest.approx(0.15)
    assert out[2].start == pytest.approx(1.2 + 0.15)
    starts = [w.start for w in out]
    assert starts == sorted(starts)
    assert all(w.end > w.start for w in out)
    # a word outside every segment is dropped
    assert remap_words([_w("x", 2.2, 2.5)], segs) == []


def test_clip_fps_never_above_source():
    assert clip_fps(60, 59.94) == 60
    assert clip_fps(60, 30.0) == 30
    assert clip_fps(30, 60.0) == 30
    assert clip_fps(60, 49.9) == 30


def _loudness(path):
    proc = subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-i", str(path), "-af", "ebur128",
                           "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", stdin=subprocess.DEVNULL)
    return float(re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", proc.stderr)[-1])


def _render(media, tmp_path, plan, profile=None):
    path, words = media
    m = _moment()
    out = tmp_path / "out" / "clip.mp4"
    wd = tmp_path / "work"
    segs = keep_segments(words, m.start, m.end)
    res = render_clip(_src(path), m, words, plan, profile or _profile(), OPTS,
                      wd, out, threading.Event(), lambda f: None)
    return res, segs, wd


def test_render_clip_exact_size_fps_and_duration(make_mp4_with_speech, tmp_path):
    plan = CropPlan("cara", 100, 178, [(0.0, 20), (2.0, 120), (5.9, 120)], False)
    out, segs, wd = _render(make_mp4_with_speech, tmp_path, plan)
    info = ffmpeg.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["fps"] == pytest.approx(30, abs=0.01)
    assert info["duration"] == pytest.approx(sum(b - a for a, b in segs), abs=1 / 30 + 0.02)
    assert not list(wd.glob("*.wav")) and not list(wd.glob("*.mp4"))  # intermediates removed


def test_render_clip_loudness_near_minus_14(make_mp4_with_speech, tmp_path):
    plan = CropPlan("centro", 100, 178, [(0.0, 110)], False)
    out, _, _ = _render(make_mp4_with_speech, tmp_path, plan)
    assert _loudness(out) == pytest.approx(-14, abs=1.5)


def test_render_audio_only_source_makes_audiogram(make_wav, tmp_path):
    wav = make_wav(seconds=4)
    words = [_w("hola", 0.5, 1.0), _w("mundo", 1.1, 1.6)]
    src = _src(wav, w=0, h=0, fps=0.0, has_video=False)
    out = render_clip(src, _moment(0.0, 3.0), words, None, _profile(), OPTS,
                      tmp_path / "work", tmp_path / "a.mp4", threading.Event(), lambda f: None)
    info = ffmpeg.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["duration"] == pytest.approx(1.4, abs=0.1)  # 0.35 .. 1.75


def test_hook_title_and_credit_events_in_ass(tmp_path):
    ass = tmp_path / "x.ass"
    style = captions.PRESETS["poster"]
    captions.write_clip_ass([_w("hola", 0.5, 1.0)], style, 1080, 1920, ass, captions.FONTS_DIR,
                            top_text="Lo que {\\b1}nadie\ndice", top_until=3.0, credit="Fuente: @canal")
    text = ass.read_text(encoding="utf-8")
    hook = [ln for ln in text.splitlines() if ln.startswith("Dialogue") and ",Hook," in ln]
    cred = [ln for ln in text.splitlines() if ln.startswith("Dialogue") and ",Credit," in ln]
    assert len(hook) == 1 and len(cred) == 1
    assert hook[0].split(",")[1:3] == ["0:00:00.00", "0:00:03.00"]
    assert "LO QUE" in hook[0]  # poster preset is uppercase
    assert "\\b1" not in hook[0] and "{{" not in hook[0]
    assert "Fuente: @canal" in cred[0] and "\\an1" in cred[0]
    assert "Style: Hook," in text and "Style: Credit," in text
    # off by default
    captions.write_clip_ass([_w("hola", 0.5, 1.0)], style, 1080, 1920, ass, captions.FONTS_DIR)
    plain = ass.read_text(encoding="utf-8")
    assert ",Hook," not in plain and ",Credit," not in plain


def test_title_and_credit_follow_profile_flags(make_mp4_with_speech, tmp_path, monkeypatch):
    seen = {}
    real = captions.write_clip_ass

    def spy(words, style, width, height, path, fonts_dir, top_text="", top_until=3.0, credit=""):
        seen.update(top=top_text, credit=credit)
        return real(words, style, width, height, path, fonts_dir, top_text, top_until, credit)

    monkeypatch.setattr(captions, "write_clip_ass", spy)
    plan = CropPlan("centro", 100, 178, [(0.0, 110)], False)
    _render(make_mp4_with_speech, tmp_path, plan)
    assert seen == {"top": "Un titulo", "credit": "Fuente: @Canal"}
    _render(make_mp4_with_speech, tmp_path, plan, profile=_profile(hook_title=False, credit=False))
    assert seen == {"top": "", "credit": ""}


def test_cancel_kills_ffmpeg(make_mp4, tmp_path):
    path = make_mp4(seconds=30, w=1280, h=720)
    src = _src(path, w=1280, h=720, fps=30.0)
    plan = CropPlan("centro", 404, 720, [(0.0, 400)], False)
    cancel = threading.Event()
    cancel.set()
    out = tmp_path / "o.mp4"
    with pytest.raises(JobCancelled):
        render_clip(src, _moment(0.0, 30.0), [], plan, _profile(), OPTS, tmp_path / "w", out, cancel,
                    lambda f: None)
    assert not out.exists()
    assert not list((tmp_path / "w").glob("*.wav"))


def test_remap_keys_follows_cuts_and_keeps_jumps():
    from baitd.clip.render import remap_keys
    segs = [(1.0, 3.0), (6.0, 8.0)]  # span starts at 0; 3..6 is cut
    keys = [(0.0, 0), (4.0, 100), (7.0, 100), (7.0, 400), (9.0, 400)]
    out = remap_keys(keys, segs, 0.0)
    assert out[0] == (0.0, 25)  # interpolated at 1.0
    assert (2.0, 75) in out and (2.0, 100) in out  # segment boundary keys at the cut
    assert not any(0.0 < t < 2.0 and x == 100 and t != 2.0 for t, x in out)  # key at 4.0 dropped
    assert out[-1] == (4.0, 400) and (3.0, 100) in out and (3.0, 400) in out  # jump kept
    assert remap_keys([(0.0, 50), (5.0, 50)], segs, 0.0) == [(0.0, 50)]


def _mp3_with_cover(tmp_path) -> Path:
    from conftest import _ff
    cover, mp3 = tmp_path / "cover.png", tmp_path / "song.mp3"
    _ff("-f", "lavfi", "-i", "color=c=red:s=600x600", "-frames:v", "1", cover)
    _ff("-f", "lavfi", "-i", "sine=f=440:d=4", "-i", cover, "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame",
        "-c:v", "mjpeg", "-disposition:v", "attached_pic", "-id3v2_version", "3", mp3)
    return mp3


def test_audio_with_cover_art_is_an_audiogram(tmp_data, tmp_path):
    from baitd.clip import sources
    sid, target = sources.new_upload("cancion.mp3")
    target.write_bytes(_mp3_with_cover(tmp_path).read_bytes())
    src = sources.finish_upload(sid, target, "cancion.mp3")
    assert src.has_video is False and (src.width, src.height) == (0, 0)  # the cover is not the video
    out = render_clip(src, _moment(0.0, 3.0), [_w("hola", 0.5, 1.0)], None, _profile(), OPTS,
                      tmp_path / "work", tmp_path / "a.mp4", threading.Event(), lambda f: None)
    info = ffmpeg.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)


def test_render_without_video_stream_is_a_user_error(make_wav, tmp_path, monkeypatch):
    from baitd.clip import render as clip_render
    from baitd.models import UserError
    monkeypatch.setattr(clip_render, "probe", lambda p: {"duration": 1.0, "width": None, "height": None, "fps": None})
    src = _src(make_wav(seconds=4), w=0, h=0, fps=0.0, has_video=False)
    out = tmp_path / "a.mp4"
    with pytest.raises(UserError) as e:
        render_clip(src, _moment(0.0, 3.0), [], None, _profile(), OPTS, tmp_path / "work", out,
                    threading.Event(), lambda f: None)
    assert "→" in str(e.value)
    assert not out.exists()
