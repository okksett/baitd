import pytest


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setenv("BAITD_DATA", str(tmp_path))
    yield tmp_path


# ---- render fixtures: generated media, offline ----
import subprocess
from pathlib import Path

from baitd import captions
from baitd.ffmpeg import ffmpeg_exe
from baitd.models import Media, Voiceover, Word


def _ff(*args) -> None:
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                   check=True, stdin=subprocess.DEVNULL)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    for name, size, dur in (("v_port.mp4", "720x1280", 4), ("v_short.mp4", "720x1280", 1),
                            ("v_land4k.mp4", "3840x2160", 3), ("v_land.mp4", "1280x720", 4)):
        _ff("-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={dur}", "-c:v", "libx264",
            "-preset", "ultrafast", "-pix_fmt", "yuv420p", d / name)
    _ff("-f", "lavfi", "-i", "testsrc2=s=1600x1200:d=1", "-frames:v", "1", d / "photo.png")
    _ff("-f", "lavfi", "-i", "sine=f=220:d=6", "-ar", "24000", d / "voice.wav")
    _ff("-f", "lavfi", "-i", "sine=f=330:d=3", "-ar", "44100", d / "music.wav")
    return d


@pytest.fixture(scope="session")
def make_media(media_dir):
    def make(name: str, kind="video", w=720, h=1280, dur=None) -> Media:
        return Media(id=name, source="test", kind=kind, url="", width=w, height=h,
                     duration=dur, path=media_dir / name)
    return make


@pytest.fixture(scope="session")
def voice_6s(media_dir):
    words = [Word(t, i * 0.5, i * 0.5 + 0.4)
             for i, t in enumerate("this is a short test of the caption burn in".split())]
    return Voiceover(path=media_dir / "voice.wav", duration=6.0, words=words, scene_bounds=[(0.0, 6.0)])


@pytest.fixture(scope="session")
def music_file(media_dir):
    return media_dir / "music.wav"


@pytest.fixture
def make_ass(voice_6s):
    def make(path: Path, width=1080, height=1920) -> Path:
        captions.write_ass(voice_6s.words, captions.PRESETS["amarillo_clasico"], 2,
                           width, height, path, captions.FONTS_DIR)
        return path
    return make
