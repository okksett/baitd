"""Single-instance guard: an exclusive lock file in data_dir() that also holds the running URL.

A second start finds the lock held, reads the URL and points the user at the first instance
instead of starting a second worker on the same queue.json. The OS drops the lock when the
process dies, so a crash never leaves a lock behind.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.request
from pathlib import Path

LOCK_NAME = "instance.lock"
# Windows locks are mandatory: the lock sits on a byte far past the URL so others can read it.
_LOCK_OFFSET = 1 << 20
_URL_RE = re.compile(r"^http://127\.0\.0\.1:\d{1,5}/$")  # only ever our own local address


class InstanceLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting; False when another process holds it."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def write_url(self, url: str) -> None:
        if self._fd is None:
            raise RuntimeError("the instance lock is not held")
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        os.write(self._fd, url.encode("ascii"))

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.ftruncate(fd, 0)  # no stale URL for the next start to read
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def read_url(path: Path) -> str:
    """The URL the running instance wrote, or "" (none yet, unreadable or not a local URL)."""
    try:
        with open(path, "rb") as f:
            url = f.read(200).decode("ascii", "replace").strip()
    except OSError:
        return ""
    return url if _URL_RE.match(url) else ""


def answers(url: str, timeout: float = 2.0) -> bool:
    """True when `url` (a local Baitd page) answers GET / with 200."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local URL only
            return resp.status == 200
    except (OSError, ValueError):
        return False
