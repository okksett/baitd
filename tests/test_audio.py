import json
import random
import re
import subprocess

import pytest

from baitd import audio
from baitd.ffmpeg import ffmpeg_exe, probe, run

DURATION = 6.0
VOICE_END = 3.0  # the fake voice talks during 0..3 s and is silent afterwards


def _gen(path, source: str, *extra: str) -> None:
    run(["-f", "lavfi", "-i", source, *extra, str(path)])


def write_sine(path, seconds: float = 1.0) -> None:
    _gen(path, f"sine=frequency=440:duration={seconds}")


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    voice, music = d / "voice.wav", d / "music.wav"
    # "voice": a tone for 3 s followed by 3 s of silence; "music": 2 s of noise (shorter than
    # the video so that the -stream_loop in music_input_args is really exercised).
    _gen(voice, f"sine=frequency=300:duration={DURATION}", "-af", f"volume=0:enable='gte(t,{VOICE_END})'")
    _gen(music, "anoisesrc=color=pink:amplitude=0.5:duration=2:sample_rate=44100:seed=1")
    return voice, music


def render_mix(tmp_path, sources, volume: float = 0.15, has_music: bool = True, voice=None):
    voice, music = (voice or sources[0]), sources[1]
    out = tmp_path / "mix.wav"
    args = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-i", str(voice)]
    if has_music:
        args += audio.music_input_args(music)
    args += ["-filter_complex", audio.mix_filter(has_music, volume, DURATION), "-map", "[aout]",
             "-t", str(DURATION + 2), "-ar", "48000", str(out)]
    run(args)
    return out


def ffmpeg_stderr(*args: str) -> str:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stderr


def measure_loudnorm(path) -> dict:
    err = ffmpeg_stderr("-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-")
    return json.loads(err[err.rindex("{"):err.rindex("}") + 1])


def mean_volume(path, start: float, end: float, flt: str = "anull") -> float:
    err = ffmpeg_stderr("-i", str(path), "-af", f"atrim={start}:{end},{flt},volumedetect", "-f", "null", "-")
    return float(re.search(r"mean_volume:\s*(-?[\d.]+) dB", err).group(1))


def true_peak(path) -> float:
    err = ffmpeg_stderr("-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-")
    return float(re.findall(r"Peak:\s*(-?[\d.]+) dBFS", err)[-1])


def test_list_music_includes_user_folder(tmp_data):
    (tmp_data / "musica").mkdir()
    write_sine(tmp_data / "musica" / "mia.mp3")
    tracks = audio.list_music()
    assert any(t["title"] == "mia" and not t["bundled"] for t in tracks)


def test_list_music_bundled_one_per_mood(tmp_data):
    bundled = [t for t in audio.list_music() if t["bundled"]]
    assert {t["mood"] for t in bundled} == {"lofi", "epico", "tranquilo", "energico"}
    for t in bundled:
        assert t["path"].is_file() and t["path"].suffix == ".mp3"
        assert set(t) == {"id", "mood", "title", "path", "bundled"}


def test_list_music_ignores_other_extensions(tmp_data):
    (tmp_data / "musica").mkdir()
    (tmp_data / "musica" / "notas.txt").write_text("x")
    assert not any(t["title"] == "notas" for t in audio.list_music())


def test_pick_track_none():
    assert audio.pick_track("none") is None


def test_pick_track_path_is_itself(tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"x")
    assert audio.pick_track(str(p)) == p


def test_missing_user_track_falls_back_to_none(tmp_data):
    # M4: a user track deleted after it was chosen no longer crashes the render
    assert audio.pick_track(str(tmp_data / "musica" / "borrada.mp3")) is None


def test_missing_bundled_track_falls_back_to_its_mood(tmp_data):
    gone = audio.MUSIC_DIR / "epico" / "ya-no-existe.mp3"
    t = audio.pick_track(str(gone), rng=random.Random(1))
    assert t is not None and t.is_file() and t.parent.name == "epico"


def test_pick_track_mood_is_deterministic_with_rng(tmp_data):
    t = audio.pick_track("lofi", rng=random.Random(1))
    assert t is not None and t.parent.name == "lofi"
    assert audio.pick_track("lofi", rng=random.Random(1)) == t


def test_pick_track_mood_chooses_among_that_mood(tmp_data, monkeypatch):
    fake = [{"id": f"lofi/{n}", "mood": "lofi", "title": n, "path": tmp_data / f"{n}.mp3", "bundled": True}
            for n in "ab"] + [{"id": "epico/c", "mood": "epico", "title": "c", "path": tmp_data / "c.mp3", "bundled": True}]
    monkeypatch.setattr(audio, "list_music", lambda: fake)
    picks = {audio.pick_track("lofi", rng=random.Random(s)).stem for s in range(20)}
    assert picks == {"a", "b"}


def test_pick_track_mood_without_tracks_is_none(monkeypatch):
    monkeypatch.setattr(audio, "list_music", lambda: [])
    assert audio.pick_track("lofi") is None


def test_music_input_args(tmp_path):
    p = tmp_path / "m.mp3"
    assert audio.music_input_args(p) == ["-stream_loop", "-1", "-i", str(p)]


def test_mix_filter_without_music_is_voice_only():
    f = audio.mix_filter(False, 0.15, 30)
    assert f.startswith("[1:a]loudnorm=I=-14:TP=-1.5:LRA=11") and f.endswith("[aout]")
    assert "2:a" not in f and "sidechaincompress" not in f


def test_mix_filter_with_music_has_required_stages():
    f = audio.mix_filter(True, 0.15, 30)
    for part in ["[1:a]loudnorm=I=-14:TP=-1.5:LRA=11", "[2:a]", "atrim=0:30", "volume=0.15",
                 "sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300",
                 "amix=inputs=2:normalize=0", "alimiter=limit=0.75:level=disabled", "afade=t=out:st=28.5:d=1.5"]:
        assert part in f
    assert f.endswith("[aout]")


def test_mix_loudness(tmp_path, sources):
    out = render_mix(tmp_path, sources)
    assert abs(float(measure_loudnorm(out)["input_i"]) - (-14)) <= 1.5


def test_voice_only_loudness(tmp_path, sources):
    out = render_mix(tmp_path, sources, has_music=False)
    assert abs(float(measure_loudnorm(out)["input_i"]) - (-14)) <= 1.5


def test_music_ducked_under_voice(tmp_path, sources):
    out = render_mix(tmp_path, sources, volume=1.0)
    # high-pass above the 300 Hz "voice" tone so only the (noise) music is measured
    under_voice = mean_volume(out, 0.5, 2.5, "highpass=f=2000")
    alone = mean_volume(out, 3.5, 4.3, "highpass=f=2000")  # voice silent, before the fade-out
    assert alone - under_voice >= 6


def test_final_mix_does_not_clip(tmp_path, sources):
    # volume=3 (above the UI range) makes the un-limited mix peak above 0 dBFS
    out = render_mix(tmp_path, sources, volume=3.0)
    assert true_peak(out) <= -1.0


def test_fades_out_at_the_end(tmp_path, sources):
    out = render_mix(tmp_path, sources, volume=1.0)
    assert mean_volume(out, DURATION - 0.3, DURATION) < mean_volume(out, DURATION - 3.0, DURATION - 2.5) - 6


@pytest.fixture
def short_voice(tmp_path):
    p = tmp_path / "short_voice.wav"  # 3 s of voice only, shorter than the 6 s video
    _gen(p, f"sine=frequency=300:duration={VOICE_END}")
    return p


def test_short_voice_music_lasts_full_duration_and_fades(tmp_path, sources, short_voice):
    out = render_mix(tmp_path, sources, volume=1.0, voice=short_voice)
    assert abs(probe(out)["duration"] - DURATION) <= 0.05
    assert mean_volume(out, 3.5, 4.3) > -40  # music still audible after the voice ended
    assert mean_volume(out, DURATION - 0.3, DURATION) < mean_volume(out, 3.5, 4.3) - 6


@pytest.fixture
def voice_to_the_end(tmp_path):
    p = tmp_path / "voice_to_end.wav"  # talks until 0.2 s before the end of the video
    _gen(p, f"sine=frequency=300:duration={DURATION}", "-af", f"volume=0:enable='gte(t,{DURATION - 0.2})'")
    return p


def test_fade_does_not_touch_the_voice(tmp_path, sources, voice_to_the_end):
    # I1: the end fade belongs to the music only; the closing words keep their level
    out = render_mix(tmp_path, sources, volume=0.15, voice=voice_to_the_end)
    band = "bandpass=f=300:width_type=h:w=100"
    mid = mean_volume(out, 2.0, 3.0, band)
    last = mean_volume(out, DURATION - 0.5, DURATION - 0.2, band)  # the last 0.5 s, while it still talks
    assert abs(last - mid) <= 1.0


def test_fade_is_on_the_music_branch():
    f = audio.mix_filter(True, 0.15, 30)
    music = next(part for part in f.split(";") if part.startswith("[2:a]"))
    assert "afade=t=out:st=28.5:d=1.5" in music and music.endswith("[m]")
    assert "afade" not in f.split(";")[-1]  # not after the mix


def test_short_voice_without_music_is_padded_to_duration(tmp_path, sources, short_voice):
    out = render_mix(tmp_path, sources, has_music=False, voice=short_voice)
    assert abs(probe(out)["duration"] - DURATION) <= 0.05
    assert mean_volume(out, 3.5, 5.5) < -60  # silent tail
