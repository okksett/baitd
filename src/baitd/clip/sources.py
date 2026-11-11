"""Clipping sources: links (yt-dlp), uploads, and audio extraction. Everything for a source lives in
data_dir()/fuentes/<id>/; the id is random hex, so titles and filenames never reach a path."""
from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import tempfile
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
QUALITIES = (1080, 1440, 2160)  # download height caps offered in Ajustes
DEFAULT_QUALITY = 1440


def format_for(height: int) -> str:
    return f"bv*[height<={height}]+ba/b[height<={height}]/b"


def _quality(settings: dict) -> int:
    value = settings.get("clip_max_height")
    return value if type(value) is int and value in QUALITIES else DEFAULT_QUALITY
_DEFAULT_BYTES_PER_S = 2_000_000
_ID = re.compile(r"[0-9a-f]{12}")
_PARTIAL = "upload.json"  # in a source folder while a chunked upload is still arriving
log = logging.getLogger(__name__)
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

_LINK_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BARE_ADDRESS = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]\S*)?$", re.IGNORECASE)
_TRAILING = ".,;:!?)]}»”’"


def normalize_url(text: str) -> str:
    """The link people meant, from what they pasted: the first http(s) link inside a text ("Mira este
    video https://youtu.be/x", several lines, a trailing period), or a bare address with https://
    added ("youtube.com/watch?v=x", "youtu.be/x"). Anything else comes back stripped, for check_url."""
    text = (text or "").strip()
    found = _LINK_IN_TEXT.search(text)
    if found:
        return found.group(0).rstrip(_TRAILING)
    if _BARE_ADDRESS.match(text):
        return "https://" + text.rstrip(_TRAILING)
    return text


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


# ---- YouTube session (cookies), for "Sign in to confirm you're not a bot" ----

COOKIE_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "safari")
_BROWSER_NAMES = {"firefox": "Firefox", "chrome": "Chrome", "edge": "Edge", "brave": "Brave", "opera": "Opera",
                  "vivaldi": "Vivaldi", "safari": "Safari"}
MAX_COOKIE_BYTES = 5 * 1024 * 1024
_COOKIE_HEADERS = ("# netscape http cookie file", "# http cookie file")


def _not_cookies() -> UserError:
    return UserError("Ese archivo no es un cookies.txt válido",
                     "Expórtalo en formato Netscape, por ejemplo con la extensión «Get cookies.txt LOCALLY»")


def check_cookie_file(value: str) -> Path:
    """The cookies.txt at `value` (quotes from "Copy as path" removed), if it is a Netscape cookie file."""
    path = Path(value.strip().strip('"').strip())
    if not path.is_file():
        raise UserError("No se encontró el archivo de cookies", "Revisa la ruta en Ajustes, sección Clipping")
    if path.stat().st_size > MAX_COOKIE_BYTES:
        raise _not_cookies()
    with open(path, encoding="utf-8", errors="replace") as fh:
        head = fh.read(64 * 1024)
    lines = [ln.strip("\r") for ln in head.splitlines() if ln.strip()]
    if lines and lines[0].lower().startswith(_COOKIE_HEADERS):
        return path
    if any(not ln.startswith("#") and len(ln.split("\t")) == 7 for ln in lines):
        return path
    raise _not_cookies()


def _cookie_error(err: BaseException, browser: str) -> UserError | None:
    """A clear message when yt-dlp could not read `browser`'s cookies; None for any other error."""
    if not browser:
        return None
    msg = str(err).lower()
    name = _BROWSER_NAMES.get(browser, browser)
    if "cookie" not in msg and "dpapi" not in msg:
        return None
    if "could not find" in msg:
        return UserError(f"No se encontró {name} en este ordenador",
                         "Elige otro navegador en Ajustes, sección Clipping, o usa un archivo cookies.txt")
    if "dpapi" in msg or "decrypt" in msg:
        return UserError(f"No se pudieron leer las cookies de {name}",
                         "En Windows, Chrome y Edge no dejan leer sus cookies: usa un archivo cookies.txt o Firefox")
    if "could not copy" in msg:
        return UserError(f"No se pudieron leer las cookies de {name}",
                         f"Cierra {name} por completo y vuelve a intentarlo, o usa un archivo cookies.txt")
    return UserError(f"No se pudieron leer las cookies de {name}",
                     "Usa un archivo cookies.txt en Ajustes, sección Clipping")


def _bot_check(err: BaseException, cookies_set: bool) -> UserError | None:
    msg = str(err).lower()
    if "not a bot" not in msg and "sign in to confirm" not in msg:
        return None
    if cookies_set:
        return UserError("YouTube pide confirmar que no eres un bot",
                         "Tu sesión de YouTube caducó: entra a YouTube en el navegador y vuelve a exportar cookies.txt")
    return UserError("YouTube pide confirmar que no eres un bot",
                     "En Ajustes, sección Clipping, elige tu navegador o un archivo cookies.txt con tu sesión de YouTube")


def _is_forbidden(err: BaseException) -> bool:
    msg = str(err).lower()
    return "403" in msg or "forbidden" in msg


def _forbidden(err: BaseException) -> UserError | None:
    """YouTube refused the video data (HTTP 403) on every attempt. Updating yt-dlp rarely helps here;
    another network, a signed-in session, or uploading the file does."""
    if not _is_forbidden(err):
        return None
    return UserError("YouTube bloqueó la descarga (error 403)",
                     "Baitd ya probó otras formas de bajarlo. Pulsa Reintentar más tarde o desde otra red "
                     "(sin VPN); si sigue, elige tu navegador o un cookies.txt en Ajustes, sección Clipping, "
                     "o sube el archivo")


# What fetch_url tries when YouTube answers 403. The video links a client gets are tied to it and to the
# connection, so one being refused says little about the others. IPv4 first: some ISPs' IPv6 ranges get
# 403s. Then clients that, on a video checked on 2026-11-11, still gave 1080p (web_embedded) or 360p.
_IPV4 = {"source_address": "0.0.0.0"}
_YOUTUBE_FALLBACKS = (
    ("IPv4 + cliente web_embedded", {**_IPV4, "extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}),
    ("IPv4 + clientes de 360p", {**_IPV4, "extractor_args": {"youtube": {
        "player_client": ["mweb", "android_vr", "tv_simply"]}}}),
)


def _attempts(url: str) -> list[tuple[str, dict]]:
    """(label for the log, extra yt-dlp options) for each try, the normal one first."""
    host = (urlparse(url).hostname or "").lower()
    youtube = any(host == d or host.endswith("." + d) for d in ("youtube.com", "youtu.be"))
    return [("normal", {}), ("IPv4", _IPV4), *(_YOUTUBE_FALLBACKS if youtube else ())]


def _download_once(ytdlp, url: str, opts: dict, folder: Path) -> dict:
    """One extract + download with `opts`; returns the video's info. Refuses live streams, videos over
    the length limit, and downloads that would not fit on the disk."""
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
    return info


def _cookie_opts(settings: dict, tmp_dir: Path) -> tuple[dict, str]:
    """yt-dlp options for the user's YouTube session and the browser in use ("" when none).

    A cookies.txt wins over a browser and is passed as a private copy: yt-dlp writes its whole cookie
    jar back to `cookiefile` on close, which would rewrite the user's export."""
    file_value = str(settings.get("yt_cookies_file") or "").strip()
    if file_value:
        copy = tmp_dir / "cookies.txt"
        shutil.copyfile(check_cookie_file(file_value), copy)
        return {"cookiefile": str(copy)}, ""
    browser = str(settings.get("yt_cookies_browser") or "")
    if browser in COOKIE_BROWSERS:
        return {"cookiesfrombrowser": (browser,)}, browser
    return {}, ""


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

    settings = config.load_settings()
    opts = {
        "format": format_for(_quality(settings)), "merge_output_format": "mp4", "ffmpeg_location": ffmpeg.ffmpeg_exe(),
        "outtmpl": str(folder / "source.%(ext)s"), "noplaylist": True, "playlist_items": "1",
        "quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 20, "retries": 3,
        "progress_hooks": [hook],
        **ytdlp_loader.js_runtime_opts(),
    }
    failures = (ytdlp.utils.DownloadError, getattr(ytdlp.utils, "YoutubeDLError", ytdlp.utils.DownloadError))
    with tempfile.TemporaryDirectory(prefix="baitd-cookies-") as cookie_tmp:
        cookie_opts, browser = _cookie_opts(settings, Path(cookie_tmp))
        opts.update(cookie_opts)
        try:
            attempts = _attempts(url)
            for n, (label, extra) in enumerate(attempts, 1):
                if cancel.is_set():
                    raise JobCancelled()
                try:
                    info = _download_once(ytdlp, url, {**opts, **extra}, folder)
                except failures as e:
                    if cancel.is_set() or not _is_forbidden(e):
                        raise
                    log.warning("YouTube respondió 403 (intento %d de %d: %s): %s · Deno: %s · yt-dlp %s",
                                n, len(attempts), label, _short_reason(e, url),
                                "sí" if opts.get("js_runtimes") else "no",
                                getattr(getattr(ytdlp, "version", None), "__version__", "?"))
                    if n == len(attempts):
                        raise
                    shutil.rmtree(folder, ignore_errors=True)
                    continue
                if n > 1:
                    log.info("Descarga lograda en el intento %d de %d (%s)", n, len(attempts), label)
                break
            path = _find_download(folder)
            if path is None:
                raise UserError("No se pudo descargar el video: no se creó ningún archivo",
                                "Actualiza yt-dlp en Ajustes o sube el archivo")
            src = _build_info(source_id, path, title=info.get("title") or "Video",
                              channel=_channel_credit(info), url=url)
            _write_meta(folder, src)
            on_progress(1.0)
            return src
        except BaseException as e:
            shutil.rmtree(folder, ignore_errors=True)
            if isinstance(e, JobCancelled):
                raise
            if cancel.is_set():  # yt-dlp may have wrapped the hook's JobCancelled in its own error
                raise JobCancelled() from e
            if isinstance(e, (*failures, OSError)):
                friendly = _cookie_error(e, browser) or _bot_check(e, bool(cookie_opts)) or _forbidden(e)
                if friendly is not None:
                    raise friendly from None
            if isinstance(e, failures):
                raise UserError(f"No se pudo descargar el video: {_short_reason(e, url)}",
                                "Actualiza yt-dlp en Ajustes o sube el archivo") from None
            raise


def _channel_credit(info: dict) -> str:
    """What the credit line shows: the channel's @handle when yt-dlp gives one, else its plain name."""
    for key in ("uploader_id", "channel", "uploader"):
        v = str(info.get(key) or "").strip()
        if v.startswith("@"):
            return v
    return str(info.get("uploader") or info.get("channel") or "").strip()


# ---- uploads ----

def upload_ext(filename: str) -> str:
    """The file's extension when UPLOAD_EXTS allows it; anything else is rejected before a byte is stored."""
    ext = Path(filename.replace("\\", "/")).suffix.lower().lstrip(".").strip()
    if ext not in UPLOAD_EXTS:
        raise _invalid_media()
    return ext


def new_upload(filename: str) -> tuple[str, Path]:
    """Reserve a source folder for an upload; returns (id, target path). Only a safe extension from
    UPLOAD_EXTS is kept from the filename."""
    ext = upload_ext(filename)
    source_id = _new_id()
    folder = _folder(source_id)
    folder.mkdir(parents=True, exist_ok=True)
    return source_id, folder / f"source.{ext}"


def begin_upload(filename: str, size: int) -> str:
    """Reserve a source folder for a chunked upload: an empty target file plus upload.json (the original name
    and declared size). Until finish_upload writes meta.json the folder is not a source; the startup sweep
    removes any left behind."""
    source_id, path = new_upload(filename)
    try:
        path.touch()
        (path.parent / _PARTIAL).write_text(json.dumps({"name": filename, "size": size, "file": path.name},
                                                       ensure_ascii=False), encoding="utf-8")
    except BaseException:
        delete_source(source_id)
        raise
    return source_id


def pending_upload(upload_id) -> tuple[Path, int, str] | None:
    """(target file, declared size, original name) of an unfinished chunked upload, else None."""
    if not valid_id(upload_id):
        return None
    folder = _folder(upload_id)
    try:
        d = json.loads((folder / _PARTIAL).read_text(encoding="utf-8"))
        path, size, name = folder / Path(d["file"]).name, d["size"], d["name"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if type(size) is not int or not isinstance(name, str) or not path.is_file():
        return None
    return path, size, name


def complete_upload(upload_id: str) -> SourceInfo:
    """All bytes are in: probe and register the source (finish_upload). Removes the folder on failure."""
    pending = pending_upload(upload_id)
    if pending is None:
        raise _invalid_media()
    path, _size, name = pending
    (path.parent / _PARTIAL).unlink()
    return finish_upload(upload_id, path, name)


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


def valid_id(source_id) -> bool:
    return isinstance(source_id, str) and bool(_ID.fullmatch(source_id))


def delete_source(source_id: str) -> None:
    if valid_id(source_id):
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
