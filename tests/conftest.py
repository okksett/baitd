import pytest


@pytest.fixture(autouse=True)
def no_real_waits(monkeypatch):
    """Retry waits must never really sleep in tests."""
    monkeypatch.setattr("baitd.llm._http._sleep", lambda seconds, cancel=None: None)


@pytest.fixture(autouse=True)
def no_real_ollama(monkeypatch):
    """No test asks a real Ollama whether it is there (a developer may have one running)."""
    monkeypatch.setattr("baitd.llm.ollama._probe", lambda root: False)
    monkeypatch.setattr("baitd.llm.ollama._found", set())


@pytest.fixture(autouse=True)
def installer_not_launched(monkeypatch):
    """A test that launches the (fake) installer must not leave later tests with a 409."""
    monkeypatch.setattr("baitd.updater.installing", False)


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
    _ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=3", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", d / "v_bt709.mp4")
    _ff("-f", "lavfi", "-i", "testsrc2=s=720x1280:r=24000/1001:d=2", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuvj420p", d / "v_full.mp4")
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


# ---- clipping fixtures: small generated media and a fake yt-dlp, all offline ----
import itertools
import shutil
import types

_SPEECH = [("esto", 0.5, 0.9), ("es", 1.0, 1.3), ("una", 1.4, 1.7), ("prueba", 1.8, 2.4),
           ("de", 3.4, 3.7), ("habla", 3.8, 4.3), ("con", 4.4, 4.7), ("pausas", 4.8, 5.4)]  # gap 2.4 -> 3.4


@pytest.fixture
def make_mp4(tmp_path):
    n = itertools.count()

    def make(seconds=10, w=1920, h=1080, fps=30) -> Path:
        out = tmp_path / "media" / f"v{next(n)}.mp4"
        out.parent.mkdir(exist_ok=True)
        _ff("-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={seconds}",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out)
        return out
    return make


@pytest.fixture
def make_wav(tmp_path):
    n = itertools.count()

    def make(seconds=10) -> Path:
        out = tmp_path / "media" / f"a{next(n)}.wav"
        out.parent.mkdir(exist_ok=True)
        _ff("-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-ar", "16000", "-ac", "1", out)
        return out
    return make


@pytest.fixture
def make_mp4_with_speech(tmp_path):
    """(path, words): a tone audible only during each word, silent between them; absolute seconds."""
    out = tmp_path / "media" / "speech.mp4"
    out.parent.mkdir(exist_ok=True)
    gate = "+".join(f"between(t,{s},{e})" for _, s, e in _SPEECH)
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=6",
        "-f", "lavfi", "-i", f"sine=f=300:r=16000:d=6,volume='gt({gate},0)':eval=frame",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out)
    return out, [Word(t, s, e) for t, s, e in _SPEECH]


class FakeYtdlp:
    """Stands in for the yt_dlp module. download() writes a small real mp4 to the outtmpl path and
    calls the progress hooks, wrapping hook errors in DownloadError like yt-dlp does."""

    def __init__(self, make_mp4):
        from yt_dlp.utils import DownloadError, YoutubeDLError
        self.utils = types.SimpleNamespace(DownloadError=DownloadError, YoutubeDLError=YoutubeDLError)
        self.info = {"id": "x", "title": "Video", "uploader": "Canal", "duration": 60, "is_live": False}
        self.error: Exception | None = None  # raised by download()
        self.extract_error: Exception | None = None  # raised by extract_info() (bot checks, cookie reads)
        self.in_download = None  # callable run between progress hooks
        self.opts: dict = {}
        self.cookiefile_seen: str | None = None  # contents of opts["cookiefile"] when yt-dlp read it
        fake = self

        class YoutubeDL:
            def __init__(self, opts):
                fake.opts = opts
                if opts.get("cookiefile"):
                    fake.cookiefile_seen = Path(opts["cookiefile"]).read_text(encoding="utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                if fake.opts.get("cookiefile"):  # real yt-dlp saves its cookie jar back to cookiefile on close
                    with open(fake.opts["cookiefile"], "a", encoding="utf-8") as fh:
                        fh.write(".youtube.com\tTRUE\t/\tTRUE\t0\tWRITTEN_BACK\t1\n")
                return False

            def extract_info(self, url, download=True):
                if fake.extract_error:
                    raise fake.extract_error
                return dict(fake.info)

            def download(self, urls):
                out = Path(fake.opts["outtmpl"].replace("%(ext)s", "mp4"))
                out.parent.mkdir(parents=True, exist_ok=True)
                try:
                    for frac in (0.25, 0.5):
                        if fake.in_download:
                            fake.in_download()
                        for hook in fake.opts["progress_hooks"]:
                            hook({"status": "downloading", "downloaded_bytes": int(frac * 100), "total_bytes": 100})
                except Exception as e:
                    raise DownloadError(str(e)) from e
                if fake.error:
                    raise fake.error
                shutil.copy(make_mp4(seconds=2, w=320, h=180, fps=15), out)
                for hook in fake.opts["progress_hooks"]:
                    hook({"status": "finished"})

        self.YoutubeDL = YoutubeDL


@pytest.fixture
def fake_ytdlp(monkeypatch, make_mp4):
    from baitd.clip import ytdlp_loader
    fake = FakeYtdlp(make_mp4)
    monkeypatch.setattr(ytdlp_loader, "import_ytdlp", lambda: fake)
    return fake
