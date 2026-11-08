"""Clipping sources: links (yt-dlp), uploads, and audio extraction. Everything for a source lives in
data_dir()/fuentes/<id>/; the id is random hex, so titles and filenames never reach a path."""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .. import config, ffmpeg
from ..ffmpeg import JobCancelled
from ..models import UserError
from . import ytdlp_loader
from .models import SourceInfo

MAX_SECONDS = 4 * 3600
UPLOAD_EXTS = ("mp4", "mov", "mkv", "webm", "m4v", "avi", "mp3", "wav", "m4a", "aac", "ogg", "opus", "flac")
FORMAT = "bv*[height<=2160]+ba/b[height<=2160]/b"
_DEFAULT_BYTES_PER_S = 2_000_000
_ID = re.compile(r"[0-9a-f]{12}")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL = re.compile(r"https?://\S+")


def sources_dir() -> Path:
    d = config.data_dir() / "fuentes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _new_id() -> str:
    return secrets.token_hex(6)


def _folder(source_id: str) -> Path:
    return sources_dir() / source_id


def _invalid_media() -> UserError:
    return UserError("El archivo no es un video o audio válido", "Prueba con MP4, MOV, MKV, WEBM, MP3 o WAV")


def _too_long() -> UserError:
    return UserError("El video dura más de 4 horas", "Recorta el video o usa uno más corto")


# ---- links ----

def check_url(url: str) -> None:
    """Reject non-http(s) links and TikTok / Instagram Reels / YouTube Shorts."""
    try:
        u = urlparse(url.strip())
        host = (u.hostname or "").lower()
    except ValueError:
        u, host = None, ""
    if u is None or u.scheme not in ("http", "https") or not host:
        raise UserError("Ese enlace no es válido", "Pega un enlace http o https de YouTube u otro sitio")

    def on(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    path = u.path.lower()
    if (on("tiktok.com")
            or (on("instagram.com") and ("/reel/" in path or "/reels/" in path))
            or (on("youtube.com") and path.startswith("/shorts/"))):
        raise UserError("Ese enlace es de TikTok/Reels/Shorts", "Usa el video largo original")


def _short_reason(err: Exception, url: str) -> str:
    """yt-dlp's message without ANSI codes, the "ERROR: " prefix, any URL or query string; <= 120 chars."""
    msg = _ANSI.sub("", str(err))
    msg = re.sub(r"^\s*ERROR:\s*", "", msg)
    msg = _URL.sub("", msg)
    query = urlparse(url).query
    for secret in (url, query):
        if secret:
            msg = msg.replace(secret, "")
    lines = [ln.strip() for ln in msg.splitlines() if ln.strip()]
    msg = re.sub(r"\s+", " ", lines[0] if lines else "").strip(" ()[]:,.")
    msg = msg or "error desconocido"
    return msg if len(msg) <= 120 else msg[:119].rstrip() + "…"


def _estimate_bytes(info: dict) -> float:
    def size(d: dict) -> float:
        return d.get("filesize") or d.get("filesize_approx") or 0

    est = size(info) or sum(size(f) for f in info.get("requested_formats") or [])
    return est or (info.get("duration") or 0) * _DEFAULT_BYTES_PER_S


def _is_live(info: dict) -> bool:
    return bool(info.get("is_live")) or info.get("live_status") in ("is_live", "is_upcoming")


def _find_download(folder: Path) -> Path | None:
    files = [p for p in folder.glob("source.*") if p.suffix.lower() not in (".part", ".ytdl", ".temp")]
    files.sort(key=lambda p: p.suffix.lower() != ".mp4")
    return files[0] if files else None


def fetch_url(url: str, cancel: threading.Event, on_progress: Callable[[float], None]) -> SourceInfo:
    url = url.strip()
    check_url(url)
    ytdlp = ytdlp_loader.import_ytdlp()
    source_id = _new_id()
    folder = _folder(source_id)

    def hook(d: dict) -> None:
        if cancel.is_set():
            raise JobCancelled()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                on_progress(min(0.99, d.get("downloaded_bytes", 0) / total))

    opts = {
        "format": FORMAT, "merge_output_format": "mp4", "ffmpeg_location": ffmpeg.ffmpeg_exe(),
        "outtmpl": str(folder / "source.%(ext)s"), "noplaylist": True, "playlist_items": "1",
        "quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 20, "retries": 3,
        "progress_hooks": [hook],
    }
    failures = (ytdlp.utils.DownloadError, getattr(ytdlp.utils, "YoutubeDLError", ytdlp.utils.DownloadError))
    try:
        if cancel.is_set():
            raise JobCancelled()
        with ytdlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            if info.get("entries"):
                info = next(iter(info["entries"]), None) or {}
            if _is_live(info):
                raise UserError("Ese video es una transmisión en vivo", "Espera a que termine y usa la grabación")
            if (info.get("duration") or 0) > MAX_SECONDS:
                raise _too_long()
            need = _estimate_bytes(info) * 1.2
            if need > free_bytes(sources_dir()):
                raise UserError(f"No hay espacio suficiente para descargar el video (~{need / 1e9:.1f} GB)",
                                "Libera espacio en el disco")
            folder.mkdir(parents=True, exist_ok=True)
            ydl.download([url])
        path = _find_download(folder)
        if path is None:
            raise UserError("No se pudo descargar el video: no se creó ningún archivo",
                            "Actualiza yt-dlp en Ajustes o sube el archivo")
        src = _build_info(source_id, path, title=info.get("title") or "Video",
                          channel=info.get("uploader") or info.get("channel") or "", url=url)
        _write_meta(folder, src)
        on_progress(1.0)
        return src
    except BaseException as e:
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(e, JobCancelled):
            raise
        if cancel.is_set():  # yt-dlp may have wrapped the hook's JobCancelled in its own error
            raise JobCancelled() from e
        if isinstance(e, failures):
            raise UserError(f"No se pudo descargar el video: {_short_reason(e, url)}",
                            "Actualiza yt-dlp en Ajustes o sube el archivo") from None
        raise


# ---- uploads ----

def new_upload(filename: str) -> tuple[str, Path]:
    """Reserve a source folder for an upload; returns (id, target path). Only a safe extension from
    UPLOAD_EXTS is kept from the filename."""
    ext = Path(filename.replace("\\", "/")).suffix.lower().lstrip(".").strip()
    if ext not in UPLOAD_EXTS:
        raise _invalid_media()
    source_id = _new_id()
    folder = _folder(source_id)
    folder.mkdir(parents=True, exist_ok=True)
    return source_id, folder / f"source.{ext}"


def finish_upload(source_id: str, path: Path, filename: str) -> SourceInfo:
    """Probe the uploaded file, enforce the limits and write meta.json. On any failure the source
    folder is removed."""
    if not _ID.fullmatch(source_id or ""):
        raise _invalid_media()
    folder = _folder(source_id)
    try:
        src = _build_info(source_id, path, title=Path(filename.replace("\\", "/")).stem or "Video",
                          channel="", url="")
        _write_meta(folder, src)
        return src
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _build_info(source_id: str, path: Path, title: str, channel: str, url: str) -> SourceInfo:
    try:
        p = ffmpeg.probe(path)
    except RuntimeError:
        raise _invalid_media() from None
    if p["duration"] > MAX_SECONDS:
        raise _too_long()
    return SourceInfo(id=source_id, path=path, title=title, channel=channel, url=url,
                      duration=float(p["duration"]), width=p["width"] or 0, height=p["height"] or 0,
                      fps=float(p["fps"] or 0), has_video=bool(p["width"] and p["height"]))


def _write_meta(folder: Path, src: SourceInfo) -> None:
    (folder / "meta.json").write_text(json.dumps(src.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# ---- stored sources ----

def load_source(source_id: str) -> SourceInfo:
    gone = UserError("Ese video ya no está disponible", "Vuelve a añadirlo con un enlace o súbelo de nuevo")
    if not isinstance(source_id, str) or not _ID.fullmatch(source_id):
        raise gone
    folder = _folder(source_id)
    try:
        src = SourceInfo.from_dict(json.loads((folder / "meta.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        raise gone from None
    src.id = source_id
    src.path = folder / src.path.name  # never trust a path stored in the file
    if not src.path.is_file():
        raise gone
    return src


def delete_source(source_id: str) -> None:
    if isinstance(source_id, str) and _ID.fullmatch(source_id):
        shutil.rmtree(_folder(source_id), ignore_errors=True)


def extract_audio(src: SourceInfo, out: Path, cancel: threading.Event) -> Path:
    """16 kHz mono 16-bit WAV of the source's first audio stream."""
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg.run(["-i", str(src.path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(out)], cancel=cancel)
    except RuntimeError:
        raise UserError("No se pudo leer el audio del video", "Comprueba que el archivo tiene sonido") from None
    return out
