"""Offline end-to-end self-test: `baitd --selftest` (also run by tests/test_smoke.py and by CI
against the frozen app).

Runs a real job through pipeline.run_job (real timeline, captions, bundled fonts and music, real
FFmpeg render) with only the network-facing steps replaced by generated ones, inside a temporary
data folder, so it never touches the user's data."""
from __future__ import annotations

import dataclasses
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import pipeline
from .clip import pipeline as clip_pipeline
from .clip import sources
from .clip.models import Transcript
from .config import default_profile
from .ffmpeg import ffmpeg_exe, has_filter, probe
from .models import Media, Scene, Script, Voiceover, Word
from .tts.timing import estimate_words

VOICE_SECONDS = 9.0
_SCENE_SECONDS = 3.0
_SCRIPT = Script(
    title="Autoprueba de Baitd", hook="Esto es una prueba.",
    scenes=[Scene("Esto es una prueba. Todo funciona sin conexión.", ["test", "video"]),
            Scene("Los clips y la voz se generan en el propio ordenador.", ["clips", "voz"]),
            Scene("Si ves este vídeo, la app está lista para usarse.", ["app", "lista"])],
    description="Vídeo de autoprueba", hashtags=["#prueba"], language="es")


CLIP_SOURCE_SECONDS = 40
_CLIP_MIN_S, _CLIP_MAX_S = 10, 20


class SelftestError(Exception):
    """The self-test failed; the message says why."""


def _ff(*args) -> None:
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise SelftestError(f"FFmpeg no pudo generar el material de prueba: {proc.stderr.strip()[-300:]}")


def _make_media(folder: Path) -> list[Media]:
    """Generated stand-ins for stock footage: a 4K landscape clip, portrait clips and a photo."""
    folder.mkdir(parents=True, exist_ok=True)
    specs = [("land4k.mp4", "video", 3840, 2160), ("port_a.mp4", "video", 720, 1280),
             ("photo.png", "photo", 1600, 1200), ("port_b.mp4", "video", 720, 1280),
             ("land_hd.mp4", "video", 1280, 720), ("port_c.mp4", "video", 720, 1280)]
    out = []
    for name, kind, w, h in specs:
        path = folder / name
        if kind == "video":
            _ff("-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d=5", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", path)
        else:
            _ff("-f", "lavfi", "-i", f"testsrc2=s={w}x{h}", "-frames:v", "1", path)
        out.append(Media(id=name, source="selftest", kind=kind, url="", width=w, height=h,
                         duration=5.0 if kind == "video" else None, path=path))
    return out


def _providers(workdir: Path) -> pipeline.Providers:
    pool = _make_media(workdir / "media")
    cursor = [0]

    def synthesize(script, profile, settings, wd):
        wav = wd / "voice.wav"
        _ff("-f", "lavfi", "-i", f"sine=f=220:d={VOICE_SECONDS}", "-ar", "48000", "-ac", "2", wav)
        bounds = [(i * _SCENE_SECONDS, (i + 1) * _SCENE_SECONDS) for i in range(len(script.scenes))]
        words = [w for scene, (s, e) in zip(script.scenes, bounds) for w in estimate_words(scene.text, s, e)]
        return Voiceover(wav, VOICE_SECONDS, words, bounds)

    def find_media(keywords, need, min_seconds, width, height, used, settings):
        picked = pool[cursor[0]:cursor[0] + need]
        cursor[0] += need
        if len(picked) < need:
            raise SelftestError("El material de prueba no alcanza para el vídeo")
        return [dataclasses.replace(m) for m in picked]

    return pipeline.Providers(generate_script=lambda topic, profile, settings: _SCRIPT,
                              synthesize=synthesize, find_media=find_media, download=lambda m: m.path)


def _check(video: Path) -> None:
    if not video.is_file():
        raise SelftestError("El vídeo de prueba no se generó")
    info = probe(video)
    if (info["width"], info["height"]) != (1080, 1920):
        raise SelftestError(f"El vídeo mide {info['width']}x{info['height']} y debería ser 1080x1920")
    if info["fps"] is None or abs(info["fps"] - 30) > 0.01:
        raise SelftestError(f"El vídeo va a {info['fps']} fps y deberían ser 30")
    if abs(info["duration"] - VOICE_SECONDS) > 0.3:
        raise SelftestError(f"El vídeo dura {info['duration']:.2f} s y la voz {VOICE_SECONDS} s")


def _clip_providers(workdir: Path) -> clip_pipeline.ClipProviders:
    """A generated 40 s 1920x1080 source, words on a known schedule (2 per second, a sentence every
    5 s), an LLM that returns two moments and no faces: nothing touches the network or a model."""
    words = [Word(f"palabra{i}." if i % 10 == 9 else f"palabra{i}", i * 0.5, i * 0.5 + 0.4) for i in range(80)]

    def fetch_url(url, cancel, on_progress):
        sid, target = sources.new_upload("autoprueba.mp4")
        _ff("-f", "lavfi", "-i", f"testsrc2=s=1920x1080:r=30:d={CLIP_SOURCE_SECONDS}",
            "-f", "lavfi", "-i", f"sine=f=220:d={CLIP_SOURCE_SECONDS}", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", target)
        on_progress(1.0)
        return sources.finish_upload(sid, target, "Autoprueba de clips.mp4")

    def complete(prompt):
        def one(start, end, score, title):
            return {"start": start, "end": end, "kind": "interesante", "score": score, "title": title,
                    "description": "Clip de autoprueba", "hashtags": ["prueba"]}
        return json.dumps({"moments": [one("00:00", "00:10", 90, "Primer clip"),
                                       one("00:20", "00:30", 80, "Segundo clip")]})

    return clip_pipeline.ClipProviders(
        fetch_url=fetch_url, complete=complete, detect_faces=lambda src, start, end, fps, cancel: [],
        transcribe=lambda audio, provider, language, settings, model, cancel, on_progress, on_model_load=None:
            Transcript("es", words, float(CLIP_SOURCE_SECONDS)))


def _check_clips(folder: Path) -> None:
    for name in ("clip-01.mp4", "clip-02.mp4"):
        clip = folder / name
        if not clip.is_file():
            raise SelftestError(f"Falta {name} en los clips de prueba")
        info = probe(clip)
        if (info["width"], info["height"]) != (1080, 1920):
            raise SelftestError(f"{name} mide {info['width']}x{info['height']} y debería ser 1080x1920")
        if info["fps"] is None or abs(info["fps"] - 30) > 0.01:
            raise SelftestError(f"{name} va a {info['fps']} fps y deberían ser 30")
        if not _CLIP_MIN_S - 0.1 <= info["duration"] <= _CLIP_MAX_S + 0.1:
            raise SelftestError(f"{name} dura {info['duration']:.2f} s y debería estar entre "
                                f"{_CLIP_MIN_S} y {_CLIP_MAX_S} s")
    if (folder / ".work").exists() or not (folder / "publicacion.txt").is_file():
        raise SelftestError("La carpeta de clips de prueba está incompleta")


def _check_clip_deps() -> None:
    """The clipping dependencies a frozen app can lose: the YuNet detector, the transcription
    libraries (imported, no model loaded) and yt-dlp."""
    try:
        import numpy as np
        from .clip import framing
        detector = framing._make_detector((320, 180))
        detector.detect(np.zeros((180, 320, 3), dtype=np.uint8))
    except Exception as e:
        raise SelftestError(f"El detector de caras no funciona: {type(e).__name__}: {e}") from e
    try:
        import ctranslate2  # noqa: F401
        import faster_whisper  # noqa: F401
    except Exception as e:
        raise SelftestError(f"Falta la transcripción local: {type(e).__name__}: {e}") from e
    try:
        from .clip import ytdlp_loader
        ytdlp_loader.import_ytdlp()
    except Exception as e:
        raise SelftestError(f"yt-dlp no se puede cargar: {type(e).__name__}: {e}") from e
    _check_deno(ytdlp_loader.deno_path())


def _check_deno(path: str | None) -> None:
    """YouTube downloads need Deno (JS challenges) and yt-dlp's solver scripts: both ship with the app."""
    if not path:
        raise SelftestError("Falta Deno (YouTube lo necesita para descargar)")
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SelftestError(f"Deno no arranca: {type(e).__name__}: {e}") from e
    if out.returncode != 0 or not out.stdout.startswith("deno "):
        raise SelftestError(f"Deno no arranca (código {out.returncode})")
    try:
        import yt_dlp_ejs  # noqa: F401
        from yt_dlp_ejs.yt import solver  # noqa: F401
    except Exception as e:
        raise SelftestError(f"Faltan los scripts de yt-dlp para YouTube: {type(e).__name__}: {e}") from e


_LIVE_T0 = 1_800_000_000.0  # an arbitrary wall-clock start for the generated live segments
_LIVE_SEG_S = 2.0


def _mp4_boxes(data: bytes) -> list[tuple[bytes, bytes]]:
    """(type, whole box bytes) of the top-level boxes of an MP4 file."""
    out, pos = [], 0
    while pos + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        if size < 8 or pos + size > len(data):
            break
        out.append((kind, data[pos:pos + size]))
        pos += size
    return out


def _check_live_cut(buf, folder: Path, name: str, what: str) -> None:
    """Cut 2 s to 6 s of the buffer into an MP4 and check it has video and about 8 s (the 4 segments)."""
    cut = buf.cut(_LIVE_T0 + 2, _LIVE_T0 + 6, folder / name)
    info = probe(cut.path)
    if not info.get("width"):
        raise SelftestError(f"El corte en directo ({what}) no tiene video")
    if abs(info["duration"] - 4 * _LIVE_SEG_S) > 0.5:
        raise SelftestError(f"El corte en directo ({what}) dura {info['duration']:.2f} s y debería durar "
                            f"unos {4 * _LIVE_SEG_S:.0f} s")


def _check_live(workdir: Path) -> None:
    """The live-clipping pieces a frozen app can lose, offline: an HLS playlist through the parser, MPEG-TS
    and fragmented-MP4 segments through the ring buffer and a cut, aiohttp, and the hotkey backend.
    No hotkey is registered and nothing touches the network."""
    from datetime import datetime, timezone
    from .live import hls, hotkey
    from .live.buffer import RingBuffer

    folder = Path(workdir) / "live"
    folder.mkdir(parents=True, exist_ok=True)

    # MPEG-TS: 4 segments of 2 s, a playlist with program-date-times, parsed back
    names = [f"seg{i}.ts" for i in range(4)]
    for i, n in enumerate(names):
        _ff("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={_LIVE_SEG_S}", "-f", "lavfi",
            "-i", f"sine=f=220:d={_LIVE_SEG_S}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", "30", "-c:a", "aac", "-shortest", "-muxdelay", "0", "-output_ts_offset", i * _LIVE_SEG_S,
            "-f", "mpegts", folder / n)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:100"]
    for i, n in enumerate(names):
        stamp = datetime.fromtimestamp(_LIVE_T0 + i * _LIVE_SEG_S, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        lines += [f"#EXT-X-PROGRAM-DATE-TIME:{stamp}", f"#EXTINF:{_LIVE_SEG_S:.3f},live", n]
    playlist = hls.parse_media_playlist(chr(10).join(lines) + chr(10), "https://live.autoprueba.test/media.m3u8")
    if len(playlist.segments) != 4 or playlist.has_map or any(s.start is None for s in playlist.segments):
        raise SelftestError("La lista de reproducción en directo de prueba no se leyó bien")
    buf = RingBuffer(folder / "ring-ts")
    for seg, n in zip(playlist.segments, names):
        buf.add(seg.seq, seg.start, seg.duration, (folder / n).read_bytes())
    _check_live_cut(buf, folder, "cut-ts.mp4", "MPEG-TS")

    # fMP4: one fragmented file split into its init (ftyp + moov) and two fragments (moof + mdat),
    # stored as init + fragment per segment, as the capture does
    frag = folder / "frag.mp4"
    _ff("-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={2 * _LIVE_SEG_S}", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", frag)
    boxes = _mp4_boxes(frag.read_bytes())
    init = b"".join(b for k, b in boxes if k in (b"ftyp", b"moov"))
    fragments = [boxes[i][1] + boxes[i + 1][1] for i, (k, _) in enumerate(boxes[:-1])
                 if k == b"moof" and boxes[i + 1][0] == b"mdat"]
    if len(fragments) < 2:
        raise SelftestError("No se pudo preparar el video fragmentado de prueba")
    buf = RingBuffer(folder / "ring-fmp4")
    for i, fr in enumerate(fragments[:2]):
        buf.add(200 + i, _LIVE_T0 + i * _LIVE_SEG_S, _LIVE_SEG_S, init + fr, "mp4")
    cut = buf.cut(_LIVE_T0 + 1, _LIVE_T0 + 3, folder / "cut-fmp4.mp4")
    info = probe(cut.path)
    if not info.get("width") or abs(info["duration"] - 2 * _LIVE_SEG_S) > 0.5:
        raise SelftestError("El corte en directo (video fragmentado) no salió bien")

    try:
        import aiohttp  # noqa: F401
    except Exception as e:
        raise SelftestError(f"Falta aiohttp, que lee el chat en directo: {type(e).__name__}: {e}") from e

    try:
        hotkey.parse_hotkey("F9")
        if sys.platform == "win32":
            if not hasattr(hotkey._user32(), "RegisterHotKey"):  # resolved, never called
                raise AttributeError("RegisterHotKey")
    except Exception as e:
        raise SelftestError(f"La tecla global no se puede preparar: {type(e).__name__}: {e}") from e


def run_selftest(workdir: Path | None = None) -> float:
    """Run the offline job; return the elapsed seconds or raise SelftestError.

    `workdir` keeps the generated files (default: a temporary folder removed afterwards)."""
    start = time.monotonic()
    if not has_filter("ass"):
        raise SelftestError("Este FFmpeg no incluye el filtro 'ass' (subtítulos)")
    previous = os.environ.get("BAITD_DATA")
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="baitd-selftest-")
        workdir = Path(tmp.name)
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["BAITD_DATA"] = str(workdir / "data")  # nothing may touch the real data folder
    try:
        profile = dataclasses.replace(default_profile(), music="lofi")  # bundled music is part of the check
        settings = {"output_dir": str(workdir / "out")}
        try:
            folder = pipeline.run_job("autoprueba", profile, settings, None, threading.Event(),
                                      lambda *a: None, _providers(workdir))
        except SelftestError:
            raise
        except Exception as e:
            raise SelftestError(f"{type(e).__name__}: {e}") from e
        _check(folder / "video.mp4")
        item = {"id": "selftest", "topic": "autoprueba", "kind": "clip_auto", "profile": profile.to_dict(),
                "payload": {"source": {"url": "https://autoprueba.test/video"},
                            "overrides": {"count": 2, "min_s": _CLIP_MIN_S, "max_s": _CLIP_MAX_S}}}
        try:
            clips = clip_pipeline.run_clip_auto(item, settings, threading.Event(), lambda *a: None,
                                                _clip_providers(workdir))
        except SelftestError:
            raise
        except Exception as e:
            raise SelftestError(f"{type(e).__name__}: {e}") from e
        _check_clips(clips)
        _check_clip_deps()
        _check_live(workdir)
        return time.monotonic() - start
    finally:
        if previous is None:
            os.environ.pop("BAITD_DATA", None)
        else:
            os.environ["BAITD_DATA"] = previous
        if tmp is not None:
            try:
                tmp.cleanup()
            except OSError:
                pass
