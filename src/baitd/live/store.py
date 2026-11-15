"""Live moments on disk. A moment cut from the live buffer is an ordinary Clipping source (the MP4, plus a
thumbnail) and an ordinary analysis marked `"origin": "live"`, so the review card and the export reuse it."""
from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .. import config, ffmpeg
from ..clip import pipeline as clip_pipeline
from ..clip import sources
from ..clip.models import FRAMINGS, KINDS, ClipProfile
from .buffer import Cut

__all__ = ["REASONS", "save_moment", "list_moments", "thumb_path", "update_moment"]

log = logging.getLogger(__name__)

REASONS = ("manual", "chat", "laughs", "loud")
THUMB = "thumb.jpg"
_EDITABLE = ("title", "description", "hashtags", "kind", "classified", "transcript")
_LOCK = threading.Lock()


def _is_channel_id(bare: str) -> bool:
    return bare.startswith("UC") and len(bare) == 24


def _credit(channel, display_name: str) -> str:
    bare = str(channel.handle).lstrip("@")
    if _is_channel_id(bare):  # a YouTube channel id says nothing to a viewer
        return display_name or bare
    return "@" + bare


def _channel_url(channel) -> str:
    url = getattr(channel, "url", "")
    if isinstance(url, str) and url:
        return url
    bare = str(channel.handle).lstrip("@")
    if channel.platform == "kick":
        return f"https://kick.com/{bare}"
    if channel.platform == "twitch":
        return f"https://www.twitch.tv/{bare}"
    if _is_channel_id(bare):
        return f"https://www.youtube.com/channel/{bare}"
    return f"https://www.youtube.com/@{bare}"


def save_moment(cut: Cut, *, channel, title: str, reason: str, at: float, session_id: str, framing: str,
                kind: str = "interesante", classified: bool = False, description: str = "",
                hashtags: Sequence[str] = (), text: str = "", display_name: str = "") -> str:
    """Store the cut as a source plus an analysis and return the analysis id. `at` is epoch seconds."""
    if reason not in REASONS:
        raise ValueError("bad reason")
    if framing not in FRAMINGS:
        raise ValueError("bad framing")
    when = datetime.fromtimestamp(at).isoformat(timespec="seconds")
    analysis_id = secrets.token_hex(6)
    src = sources.add_local_source(cut.path, title, "", _channel_url(channel))
    try:
        _thumbnail(src.path, (cut.start + cut.end) / 2)
        analysis = {
            "source_id": src.id, "profile": ClipProfile().to_dict(),
            "created_at": datetime.now().isoformat(timespec="seconds"), "origin": "live",
            "transcript": {"language": "", "words": [], "duration": cut.duration},
            "moments": [{
                "start": cut.start, "end": cut.end, "kind": kind, "score": 0, "title": title,
                "description": description, "hashtags": list(hashtags), "text": text, "framing": framing,
                "credit": "" if channel.mine else _credit(channel, display_name),
                "badge": "" if channel.mine else channel.platform, "cam": None}],
            "live": {"platform": channel.platform, "handle": channel.handle, "mine": bool(channel.mine),
                     "reason": reason, "at": when, "session": session_id, "classified": bool(classified),
                     "gap": bool(cut.gap)},
        }
        clip_pipeline._validate_analysis(analysis)
        config._write_json(clip_pipeline.analyses_dir() / f"{analysis_id}.json", analysis)
    except BaseException:
        sources.delete_source(src.id)
        raise
    return analysis_id


def _thumbnail(video: Path, mid: float) -> None:
    """One frame at the moment's midpoint; a failure only means the card has no picture."""
    try:
        ffmpeg.run(["-ss", f"{max(mid, 0.0):.3f}", "-i", str(video), "-vf", "scale=320:-2",
                    "-frames:v", "1", str(video.parent / THUMB)], low_priority=True)
    except RuntimeError as e:
        log.warning("No se pudo crear la miniatura del momento: %s", e)


def _read(file: Path) -> dict | None:
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("origin") == "live" else None


def list_moments() -> list[dict]:
    """Every stored live moment, newest first."""
    found = []
    for f in clip_pipeline.analyses_dir().glob("*.json"):
        raw = _read(f)
        if raw is None or not clip_pipeline._valid_id(f.stem):
            continue
        try:
            live, moment = raw["live"], raw["moments"][0]
            source_id = raw["source_id"]
            entry = {
                "analysis_id": f.stem, "source_id": source_id, "title": moment["title"],
                "reason": live["reason"], "at": live["at"], "kind": moment["kind"],
                "classified": bool(live["classified"]), "platform": live["platform"],
                "handle": live["handle"], "mine": bool(live["mine"]),
                "duration": float(raw["transcript"]["duration"]), "gap": bool(live["gap"]),
                "has_thumb": (sources.sources_dir() / source_id / THUMB).is_file()}
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        found.append((entry["at"], raw.get("created_at", ""), entry))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [e for *_, e in found]


def thumb_path(analysis_id: str) -> Path | None:
    """The moment's thumbnail file, or None."""
    if not clip_pipeline._valid_id(analysis_id):
        return None
    raw = _read(clip_pipeline.analyses_dir() / f"{analysis_id}.json")
    if raw is None or not sources.valid_id(raw.get("source_id")):
        return None
    path = sources.sources_dir() / raw["source_id"] / THUMB
    return path if path.is_file() else None


def update_moment(analysis_id: str, **fields) -> None:
    """Edit title, description, hashtags, kind, classified or transcript of a stored moment; anything else
    raises ValueError. `transcript` is the analysis's; `classified` lives in the live block; the rest are
    fields of its single moment."""
    unknown = set(fields) - set(_EDITABLE)
    if unknown:
        raise ValueError("cannot edit: " + ", ".join(sorted(unknown)))
    for key in ("title", "description"):
        if key in fields and not isinstance(fields[key], str):
            raise ValueError(f"{key} must be text")
    if "kind" in fields and fields["kind"] not in KINDS:
        raise ValueError("bad kind")
    if "classified" in fields and type(fields["classified"]) is not bool:
        raise ValueError("classified must be true or false")
    if "hashtags" in fields:
        tags = fields["hashtags"]
        if not isinstance(tags, (list, tuple)) or not all(isinstance(t, str) for t in tags):
            raise ValueError("hashtags must be a list of text")
        fields["hashtags"] = list(tags)
    if "transcript" in fields:
        clip_pipeline._transcript_from_dict(fields["transcript"])
    with _LOCK:
        data = clip_pipeline.load_analysis(analysis_id)
        if data.get("origin") != "live":
            raise ValueError("not a live moment")
        for key, value in fields.items():
            if key == "transcript":
                data["transcript"] = value
            elif key == "classified":
                data["live"]["classified"] = value
            else:
                data["moments"][0][key] = value
        clip_pipeline._validate_analysis(data)
        config._write_json(clip_pipeline.analyses_dir() / f"{analysis_id}.json", data)
