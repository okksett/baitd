"""Thin wrapper around the FFmpeg binary shipped by imageio-ffmpeg (no ffprobe)."""
from __future__ import annotations

import re
import subprocess
import threading
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Callable

import imageio_ffmpeg


class JobCancelled(Exception):
    """Raised when a job is cancelled via its cancel event."""


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(
    args: list[str],
    cwd: Path | None = None,
    cancel: threading.Event | None = None,
    on_line: Callable[[str], None] | None = None,
) -> str:
    """Run ffmpeg with `args`; return stderr. Raise RuntimeError on failure, JobCancelled on cancel."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", *args]
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines: list[str] = []
    tail: deque[str] = deque(maxlen=20)

    def _reader() -> None:
        # ffmpeg progress uses \r; split on both \r and \n
        buf = b""
        assert proc.stderr is not None
        while True:
            chunk = proc.stderr.read(1)
            if not chunk:
                break
            if chunk in (b"\r", b"\n"):
                if buf:
                    _emit(buf)
                    buf = b""
            else:
                buf += chunk
        if buf:
            _emit(buf)

    def _emit(raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace")
        lines.append(line)
        tail.append(line)
        if on_line:
            try:
                on_line(line)
            except Exception:
                pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    reader.join(timeout=2)
                    raise JobCancelled()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    reader.join(timeout=5)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed (exit %d):\n%s" % (proc.returncode, "\n".join(tail)))
    return "\n".join(lines)


def probe(path: Path) -> dict:
    """Return duration (s), width, height, fps by parsing `ffmpeg -i` stderr."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    err = proc.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError("Cannot read media file %s:\n%s" % (path, "\n".join(err.splitlines()[-20:])))
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    width = height = fps = None
    v = re.search(r"Stream #\S+.*?Video:.*", err)
    if v:
        line = v.group(0)
        d = re.search(r"[\s,](\d{2,5})x(\d{2,5})(?=[\s,\[])", line)
        if d:
            width, height = int(d.group(1)), int(d.group(2))
        f = re.search(r"([\d.]+)\s*fps", line) or re.search(r"([\d.]+)\s*tbr", line)
        if f:
            fps = float(f.group(1))
    return {"duration": duration, "width": width, "height": height, "fps": fps}


@lru_cache(maxsize=None)
def _filters_text() -> str:
    return subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def has_filter(name: str) -> bool:
    return re.search(r"^\s*\S+\s+%s\s" % re.escape(name), _filters_text(), re.MULTILINE) is not None


_HW_ENCODERS = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"]


@lru_cache(maxsize=None)
def pick_encoder() -> str:
    """First hardware H.264 encoder whose 1-frame test encode works, else libx264."""
    for enc in _HW_ENCODERS:
        try:
            run(
                ["-f", "lavfi", "-i", "color=c=black:s=256x256:r=30:d=0.1", "-frames:v", "1",
                 "-pix_fmt", "yuv420p", "-c:v", enc, "-f", "null", "-"]
            )
            return enc
        except Exception:
            continue
    return "libx264"
