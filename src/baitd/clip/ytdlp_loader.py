"""Loads yt-dlp, preferring a user-updated copy (data_dir()/yt-dlp.zip) over the installed package.

The release asset `yt-dlp` is a zipapp: a shebang line followed by a zip. zipimport accepts such a
prefixed zip on sys.path, so updating is just replacing one verified file."""
from __future__ import annotations

import hashlib
import importlib
import os
import re
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import requests

from .. import config
from ..ffmpeg import JobCancelled
from ..models import UserError

RELEASE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
ZIP_NAME = "yt-dlp.zip"
_MARKER = "yt_dlp/__init__.py"
_CHUNK = 64 * 1024


def zip_path() -> Path:
    return config.data_dir() / ZIP_NAME


def _zip_ok(path: Path) -> bool:
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as z:
            return _MARKER in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _zip_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", z.read("yt_dlp/version.py").decode("utf-8"))
            return m.group(1) if m else "desconocida"
    except (OSError, KeyError, zipfile.BadZipFile):
        return "desconocida"


def import_ytdlp():
    """Return the yt_dlp module. The updated zip goes first on sys.path, but only if it is valid
    and yt_dlp has not been imported yet (a module cannot be swapped once loaded)."""
    if "yt_dlp" not in sys.modules:
        z = zip_path()
        if z.is_file() and _zip_ok(z) and str(z) not in sys.path:
            sys.path.insert(0, str(z))
            importlib.invalidate_caches()
    return importlib.import_module("yt_dlp")


def _stream(url: str, cancel: threading.Event | None, sink) -> None:
    with requests.get(url, stream=True, timeout=(10, 30)) as r:
        r.raise_for_status()
        for chunk in r.iter_content(_CHUNK):
            if cancel is not None and cancel.is_set():
                raise JobCancelled()
            if chunk:
                sink(chunk)


def _expected_hash(sums: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == "yt-dlp":
            return parts[0].lower()
    return None


def update_ytdlp(cancel: threading.Event | None = None) -> str:
    """Download the latest yt-dlp release, verify it against SHA2-256SUMS and install it atomically
    as data_dir()/yt-dlp.zip. Returns the new version. It takes effect after a restart."""
    bad_verify = UserError("No se pudo verificar la actualización de yt-dlp", "Inténtalo de nuevo más tarde")
    tmp: Path | None = None
    try:
        sums = bytearray()
        _stream(RELEASE_URL + "SHA2-256SUMS", cancel, sums.extend)
        expected = _expected_hash(sums.decode("utf-8", errors="replace"))
        if not expected:
            raise bad_verify
        digest = hashlib.sha256()
        fd, name = tempfile.mkstemp(prefix="yt-dlp-", suffix=".part", dir=config.data_dir())
        tmp = Path(name)
        with os.fdopen(fd, "wb") as f:
            def sink(chunk: bytes) -> None:
                digest.update(chunk)
                f.write(chunk)

            _stream(RELEASE_URL + "yt-dlp", cancel, sink)
        if digest.hexdigest() != expected or not _zip_ok(tmp):
            raise bad_verify
        os.replace(tmp, zip_path())
        tmp = None
        return _zip_version(zip_path())
    except requests.RequestException as e:
        raise UserError("No se pudo descargar la actualización de yt-dlp",
                        "Revisa tu conexión e inténtalo de nuevo") from e
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
