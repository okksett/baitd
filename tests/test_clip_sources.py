import hashlib
import io
import json
import subprocess
import sys
import threading
import zipfile

import pytest
import requests

from baitd import ffmpeg
from baitd.clip import sources, ytdlp_loader
from baitd.ffmpeg import JobCancelled
from baitd.models import UserError


def _go(url="https://youtube.com/watch?v=x", cancel=None):
    return sources.fetch_url(url, cancel or threading.Event(), lambda p: None)


@pytest.mark.parametrize("url", ["https://www.tiktok.com/@a/video/1", "https://vm.tiktok.com/x",
    "https://www.instagram.com/reel/abc/", "https://www.instagram.com/reels/abc/",
    "https://www.youtube.com/shorts/abc"])
def test_blocked_links(url):
    with pytest.raises(UserError, match="TikTok/Reels/Shorts"):
        sources.check_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/a", "file:///c:/a.mp4", "javascript:alert(1)", "nonsense", ""])
def test_invalid_links(url):
    with pytest.raises(UserError, match="no es válido"):
        sources.check_url(url)


def test_normal_links_pass():
    sources.check_url("https://www.youtube.com/watch?v=abc")
    sources.check_url("https://www.instagram.com/p/abc/")
    sources.check_url("http://vimeo.com/123")


def test_fetch_uses_id_not_title_in_path(fake_ytdlp, tmp_data):
    fake_ytdlp.info = {"id": "x", "title": "日本語 🎉 / ..", "uploader": "Canal", "duration": 60, "is_live": False}
    s = _go()
    assert s.path.parent.name == s.id and len(s.id) == 12 and "日本" not in str(s.path)
    assert s.channel == "Canal" and s.title == "日本語 🎉 / .." and s.has_video and s.width == 320
    assert "source.%(ext)s" in fake_ytdlp.opts["outtmpl"] and "日本" not in fake_ytdlp.opts["outtmpl"]
    assert fake_ytdlp.opts["noplaylist"] and "cookiefile" not in fake_ytdlp.opts
    assert "bv*[height<=2160]" in fake_ytdlp.opts["format"]
    assert sources.load_source(s.id) == s


def test_fetch_reports_progress(fake_ytdlp, tmp_data):
    seen = []
    sources.fetch_url("https://youtube.com/watch?v=x", threading.Event(), seen.append)
    assert seen[0] == 0.25 and seen[-1] == 1.0 and seen == sorted(seen)


def test_fetch_rejects_over_4h_and_live(fake_ytdlp, tmp_data):
    fake_ytdlp.info = {"title": "t", "duration": 4 * 3600 + 1}
    with pytest.raises(UserError, match="más de 4 horas"):
        _go()
    for live in ({"is_live": True, "duration": None}, {"live_status": "is_live"}, {"live_status": "is_upcoming"}):
        fake_ytdlp.info = {"title": "t", **live}
        with pytest.raises(UserError, match="transmisión en vivo"):
            _go()
    assert list((tmp_data / "fuentes").iterdir()) == []


def test_fetch_checks_disk_space(fake_ytdlp, tmp_data, monkeypatch):
    monkeypatch.setattr(sources, "free_bytes", lambda p: 10)
    with pytest.raises(UserError, match="espacio") as e:
        _go()
    assert "GB" in e.value.cause and e.value.fix == "Libera espacio en el disco"
    assert list((tmp_data / "fuentes").iterdir()) == []


def test_disk_estimate_prefers_filesize(fake_ytdlp, tmp_data, monkeypatch):
    fake_ytdlp.info = {"title": "t", "duration": 60, "filesize": 5000}
    monkeypatch.setattr(sources, "free_bytes", lambda p: 5999)
    with pytest.raises(UserError, match="espacio"):
        _go()
    monkeypatch.setattr(sources, "free_bytes", lambda p: 6001)
    _go()


def test_cancel_during_download_removes_folder(fake_ytdlp, tmp_data):
    cancel = threading.Event()
    fake_ytdlp.in_download = cancel.set  # the progress hook raises JobCancelled; yt-dlp wraps it
    with pytest.raises(JobCancelled):
        _go(cancel=cancel)
    assert list((tmp_data / "fuentes").iterdir()) == []


def test_cancel_already_set_does_nothing(fake_ytdlp, tmp_data):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        _go(cancel=cancel)


def test_download_error_message_has_no_url(fake_ytdlp, tmp_data):
    url = "https://youtube.com/watch?v=x&token=SECRET123"
    fake_ytdlp.error = fake_ytdlp.utils.DownloadError(
        "\x1b[0;31mERROR:\x1b[0m [youtube] x: Video unavailable (see %s) via %s" % (url, url))
    with pytest.raises(UserError) as e:
        _go(url)
    msg = str(e.value)
    assert "Video unavailable" in e.value.cause and e.value.fix == "Actualiza yt-dlp en Ajustes o sube el archivo"
    assert "youtube.com" not in msg and "SECRET" not in msg and "v=x" not in msg and "\x1b" not in msg
    assert "ERROR" not in e.value.cause and len(e.value.cause) <= len("No se pudo descargar el video: ") + 120
    assert list((tmp_data / "fuentes").iterdir()) == []


def test_download_error_long_message_is_truncated(fake_ytdlp, tmp_data):
    fake_ytdlp.error = fake_ytdlp.utils.DownloadError("ERROR: " + "boom " * 100)
    with pytest.raises(UserError) as e:
        _go()
    assert len(e.value.cause) <= len("No se pudo descargar el video: ") + 120


def test_upload_roundtrip_and_audio_only(tmp_data, make_wav, make_mp4):
    sid, target = sources.new_upload("Mi Video ñ.MP4")
    assert target.name == "source.mp4" and target.parent.name == sid
    target.write_bytes(make_mp4(seconds=2, w=320, h=180).read_bytes())
    s = sources.finish_upload(sid, target, "Mi Video ñ.MP4")
    assert s.title == "Mi Video ñ" and s.has_video and s.width == 320 and s.channel == "" and s.url == ""
    assert abs(s.duration - 2) < 0.5
    assert sources.load_source(sid) == s

    aid, atarget = sources.new_upload("charla.wav")
    atarget.write_bytes(make_wav(seconds=3).read_bytes())
    a = sources.finish_upload(aid, atarget, "charla.wav")
    assert not a.has_video and a.width == 0 and a.height == 0 and abs(a.duration - 3) < 0.5

    sources.delete_source(sid)
    assert not (tmp_data / "fuentes" / sid).exists()
    with pytest.raises(UserError, match="ya no está disponible"):
        sources.load_source(sid)


def test_invalid_upload_message(tmp_data):
    sid, target = sources.new_upload("notas.mp4")
    target.write_text("esto no es un video")
    with pytest.raises(UserError) as e:
        sources.finish_upload(sid, target, "notas.mp4")
    assert e.value.cause == "El archivo no es un video o audio válido"
    assert e.value.fix == "Prueba con MP4, MOV, MKV, WEBM, MP3 o WAV"
    assert not (tmp_data / "fuentes" / sid).exists()


@pytest.mark.parametrize("name", ["virus.exe", "noext", "a.mp4.exe", "..\\..\\x.php", ""])
def test_unknown_upload_extension_rejected(tmp_data, name):
    with pytest.raises(UserError, match="no es un video o audio válido"):
        sources.new_upload(name)


def test_upload_path_ignores_filename(tmp_data):
    sid, target = sources.new_upload("../../../etc/passwd.MKV")
    assert target == tmp_data / "fuentes" / sid / "source.mkv"


def test_upload_over_4h_rejected(tmp_data, make_mp4, monkeypatch):
    monkeypatch.setattr(sources, "MAX_SECONDS", 1)
    sid, target = sources.new_upload("a.mp4")
    target.write_bytes(make_mp4(seconds=3, w=160, h=90).read_bytes())
    with pytest.raises(UserError, match="más de 4 horas"):
        sources.finish_upload(sid, target, "a.mp4")
    assert not (tmp_data / "fuentes" / sid).exists()


@pytest.mark.parametrize("bad", ["../x", "..", "ABCDEF123456", "abc", "0123456789abc", "", "0123456789a/"])
def test_source_ids_are_validated(tmp_data, bad):
    victim = tmp_data / "victim"
    victim.mkdir()
    with pytest.raises(UserError):
        sources.load_source(bad)
    sources.delete_source(bad)
    sources.delete_source("../victim")
    assert victim.exists()


def test_load_source_ignores_stored_path(tmp_data, make_mp4):
    sid, target = sources.new_upload("a.mp4")
    target.write_bytes(make_mp4(seconds=1, w=160, h=90).read_bytes())
    sources.finish_upload(sid, target, "a.mp4")
    meta = tmp_data / "fuentes" / sid / "meta.json"
    d = json.loads(meta.read_text(encoding="utf-8"))
    d["path"] = str(tmp_data / "elsewhere" / "source.mp4")
    meta.write_text(json.dumps(d), encoding="utf-8")
    assert sources.load_source(sid).path == target


def test_extract_audio_is_16k_mono(tmp_data, make_mp4):
    sid, target = sources.new_upload("a.mp4")
    target.write_bytes(make_mp4(seconds=2, w=320, h=180).read_bytes())
    s = sources.finish_upload(sid, target, "a.mp4")
    out = sources.extract_audio(s, tmp_data / "work" / "audio.wav", threading.Event())
    err = ffmpeg.run(["-i", str(out), "-f", "null", "-"])
    assert "Audio: pcm_s16le" in err and "16000 Hz, mono" in err
    assert abs(ffmpeg.probe(out)["duration"] - 2) < 0.3


def test_extract_audio_without_audio_stream(tmp_data):
    sid, target = sources.new_upload("a.mp4")
    subprocess.run([ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=160x90:d=1", "-pix_fmt", "yuv420p", str(target)], check=True)
    s = sources.finish_upload(sid, target, "a.mp4")
    with pytest.raises(UserError, match="audio"):
        sources.extract_audio(s, tmp_data / "audio.wav", threading.Event())


# ---- yt-dlp updater ----

def _zipapp(version="2099.01.01") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("yt_dlp/__init__.py", "")
        z.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
    return b"#!/usr/bin/env python3\n" + buf.getvalue()


class _Resp:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i:i + n]


def _serve(monkeypatch, binary: bytes, sums: str):
    def get(url, **kw):
        assert kw.get("stream") and kw.get("timeout")
        if url.endswith("SHA2-256SUMS"):
            return _Resp(sums.encode())
        assert url.endswith("/latest/download/yt-dlp")
        return _Resp(binary)
    monkeypatch.setattr(ytdlp_loader.requests, "get", get)


def test_update_rejects_bad_hash(tmp_data, monkeypatch):
    _serve(monkeypatch, _zipapp(), "0" * 64 + "  yt-dlp\n")
    with pytest.raises(UserError, match="verificar"):
        ytdlp_loader.update_ytdlp()
    assert list(tmp_data.glob("yt-dlp*")) == []


def test_update_rejects_missing_hash_line_and_non_zip(tmp_data, monkeypatch):
    data = _zipapp()
    _serve(monkeypatch, data, hashlib.sha256(data).hexdigest() + "  yt-dlp_linux\n")
    with pytest.raises(UserError, match="verificar"):
        ytdlp_loader.update_ytdlp()
    junk = b"not a zip"
    _serve(monkeypatch, junk, hashlib.sha256(junk).hexdigest() + "  yt-dlp\n")
    with pytest.raises(UserError, match="verificar"):
        ytdlp_loader.update_ytdlp()
    assert list(tmp_data.glob("yt-dlp*")) == []


def test_update_installs_verified_zip(tmp_data, monkeypatch):
    data = _zipapp("2099.01.01")
    sums = f"{'f' * 64}  yt-dlp.exe\n{hashlib.sha256(data).hexdigest()}  yt-dlp\n"
    _serve(monkeypatch, data, sums)
    assert ytdlp_loader.update_ytdlp() == "2099.01.01"
    assert (tmp_data / "yt-dlp.zip").read_bytes() == data
    assert list(tmp_data.glob("*.part")) == []
    assert ytdlp_loader._zip_ok(tmp_data / "yt-dlp.zip")


def test_update_network_error_and_cancel(tmp_data, monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("no net")
    monkeypatch.setattr(ytdlp_loader.requests, "get", boom)
    with pytest.raises(UserError, match="descargar la actualización"):
        ytdlp_loader.update_ytdlp()
    data = _zipapp()
    _serve(monkeypatch, data, hashlib.sha256(data).hexdigest() + "  yt-dlp\n")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        ytdlp_loader.update_ytdlp(cancel)
    assert list(tmp_data.glob("yt-dlp*")) == []


def test_import_ytdlp_ignores_invalid_zip(tmp_data):
    (tmp_data / "yt-dlp.zip").write_bytes(b"garbage")
    before = list(sys.path)
    assert ytdlp_loader.import_ytdlp().__name__ == "yt_dlp"
    assert sys.path == before


def test_speech_fixture_is_silent_between_words(make_mp4_with_speech):
    path, words = make_mp4_with_speech
    assert max(b.start - a.end for a, b in zip(words, words[1:])) > 0.6

    def peak(a, b):
        err = ffmpeg.run(["-ss", str(a), "-t", str(b - a), "-i", str(path), "-vn",
                          "-af", "volumedetect", "-f", "null", "-"])
        return float(err.split("max_volume:")[1].split("dB")[0])
    assert peak(words[1].start + 0.05, words[1].end - 0.05) > -20
    assert peak(words[3].end + 0.1, words[4].start - 0.1) < -60


@pytest.mark.parametrize("info,expected", [
    ({"uploader": "Canal Uno", "uploader_id": "@canaluno"}, "@canaluno"),
    ({"uploader": "Canal Uno", "channel": "Otro", "uploader_id": "UC123"}, "Canal Uno"),
    ({"channel": "Solo Canal"}, "Solo Canal"),
    ({"uploader": "", "channel": "@manejo"}, "@manejo"),
    ({}, ""),
])
def test_channel_credit_prefers_handle(info, expected):
    assert sources._channel_credit(info) == expected
