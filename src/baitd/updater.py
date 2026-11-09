"""Update notice and one-click update (Windows installer only).

Only URLs under DOWNLOAD_PREFIX are ever downloaded, the installer is checked against the release's
SHA256SUMS, and it runs only after that check passes."""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import requests

from .models import UserError

log = logging.getLogger("baitd")

REPO = "okksett/baitd"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
PAGE_PREFIX = f"https://github.com/{REPO}/"
SETUP_NAME = "Baitd-Setup.exe"
SUMS_NAME = "SHA256SUMS"
_CHUNK = 64 * 1024

state: dict = {"latest": None}


def parse_version(s: str) -> tuple[int, ...]:
    """'v0.3.1' -> (0, 3, 1). Anything that is not dot-separated numbers gives ()."""
    if not isinstance(s, str):
        return ()
    parts = s.strip().removeprefix("v").removeprefix("V").split(".")
    if not all(p.isascii() and p.isdigit() for p in parts):
        return ()
    return tuple(int(p) for p in parts)


def _asset_url(assets, name: str) -> str:
    for a in assets if isinstance(assets, list) else []:
        if isinstance(a, dict) and a.get("name") == name:
            url = a.get("browser_download_url")
            if isinstance(url, str) and url.startswith(DOWNLOAD_PREFIX):
                return url
    return ""


def check(current: str, get=requests.get) -> dict | None:
    """The newest release when it is newer than `current`; None when up to date or on any failure."""
    try:
        r = get(LATEST_API, timeout=5, headers={"Accept": "application/vnd.github+json",
                                                "User-Agent": f"Baitd/{current}"})
        r.raise_for_status()
        data = r.json()
        latest, now = parse_version(data["tag_name"]), parse_version(current)
        if not latest or not now or latest <= now:
            return None
        page = data.get("html_url")
        if not isinstance(page, str) or not page.startswith(PAGE_PREFIX):
            page = f"{PAGE_PREFIX}releases/latest"
        assets = data.get("assets")
        return {"version": ".".join(map(str, latest)), "page": page,
                "setup_url": _asset_url(assets, SETUP_NAME), "sums_url": _asset_url(assets, SUMS_NAME)}
    except Exception:  # offline, rate limited, odd JSON: the notice is optional
        log.debug("update check failed", exc_info=True)
        return None


def start_background_check(current: str) -> threading.Thread:
    def run() -> None:
        state["latest"] = check(current)

    t = threading.Thread(target=run, name="update-check", daemon=True)
    t.start()
    return t


def can_install(latest: dict | None) -> bool:
    return bool(latest and sys.platform == "win32" and getattr(sys, "frozen", False)
                and latest.get("setup_url") and latest.get("sums_url"))


def _expected_hash(sums: str, name: str) -> str | None:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def download_and_verify(latest: dict, dest_dir: Path, get=requests.get) -> Path:
    """Download the installer into dest_dir and return it once its SHA-256 matches the release's
    SHA256SUMS. Earlier downloads in dest_dir are removed first."""
    bad_verify = UserError("No se pudo verificar la actualización", "Descárgala desde la página de Baitd")
    setup_url, sums_url = latest.get("setup_url") or "", latest.get("sums_url") or ""
    if not (setup_url.startswith(DOWNLOAD_PREFIX) and sums_url.startswith(DOWNLOAD_PREFIX)):
        raise bad_verify
    version = ".".join(map(str, parse_version(str(latest.get("version", ""))))) or "nueva"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for old in dest_dir.iterdir():  # old installers would pile up (about 200 MB each)
        if old.is_file():
            old.unlink(missing_ok=True)
    part = dest_dir / f"Baitd-Setup-{version}.exe.part"
    final = dest_dir / f"Baitd-Setup-{version}.exe"
    try:
        try:
            with get(sums_url, timeout=(10, 30)) as r:
                r.raise_for_status()
                expected = _expected_hash(r.text, SETUP_NAME)
            if not expected:
                raise bad_verify
            digest = hashlib.sha256()
            with get(setup_url, stream=True, timeout=(10, 30)) as r, open(part, "wb") as f:
                r.raise_for_status()
                for chunk in r.iter_content(_CHUNK):
                    if chunk:
                        digest.update(chunk)
                        f.write(chunk)
        except requests.RequestException as e:
            raise UserError("No se pudo descargar la actualización",
                            "Inténtalo de nuevo o descárgala desde la página de Baitd") from e
        if digest.hexdigest() != expected:
            raise bad_verify
        os.replace(part, final)
        return final
    finally:
        part.unlink(missing_ok=True)


def launch_installer(path: Path) -> None:
    """Start the verified installer detached from this process (it closes and reopens Baitd)."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"],
                     close_fds=True, creationflags=flags)
