"""The cam (or avatar) box remembered per channel: every clip of that channel is framed as "partido" with it.

Boxes are stored as fractions of the source width and height, so a 720p and a 1080p capture share one box.
"""
from __future__ import annotations

import math

from .. import config

__all__ = ["channel_key", "load", "remember", "forget", "scaled"]


def _path():
    return config.data_dir() / "camaras.json"


def channel_key(name: str) -> str:
    """"@Rubius", "rubius" and " Rubius " are one channel (a VOD's credit and a live handle)."""
    return str(name or "").strip().lstrip("@").casefold()


def _valid(box) -> bool:
    return (isinstance(box, list) and len(box) == 4
            and all(type(v) in (int, float) and math.isfinite(v) for v in box)
            and box[0] >= 0 and box[1] >= 0 and box[2] > 0 and box[3] > 0
            and box[0] + box[2] <= 1 + 1e-6 and box[1] + box[3] <= 1 + 1e-6)


def load(name: str) -> list[float] | None:
    """The channel's box as [x, y, w, h] fractions, or None (also for a damaged file or entry)."""
    key = channel_key(name)
    if not key:
        return None
    with config._LOCK:
        box = config._read_json(_path(), {}, dict).get(key)
    return box if _valid(box) else None


def remember(name: str, cam: list[int], width: int, height: int) -> None:
    """Keep `cam` ([x, y, w, h] in pixels of a width x height source) for the channel."""
    key = channel_key(name)
    box = [cam[0] / width, cam[1] / height, cam[2] / width, cam[3] / height]
    if not key or not _valid(box):
        return
    with config._LOCK:
        data = config._read_json(_path(), {}, dict)
        data[key] = box
        config._write_json(_path(), data)


def forget(name: str) -> None:
    key = channel_key(name)
    with config._LOCK:
        data = config._read_json(_path(), {}, dict)
        if data.pop(key, None) is not None:
            config._write_json(_path(), data)


def scaled(box: list[float], width: int, height: int) -> list[int]:
    """Fractions -> [x, y, w, h] pixels of a width x height source, inside the frame."""
    w, h = max(2, round(box[2] * width)), max(2, round(box[3] * height))
    x, y = min(round(box[0] * width), width - w), min(round(box[1] * height), height - h)
    return [max(0, x), max(0, y), min(w, width), min(h, height)]
