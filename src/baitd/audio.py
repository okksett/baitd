"""Music library (bundled CC0 tracks + user folder) and the FFmpeg audio-mix filter."""
from __future__ import annotations

import random
from pathlib import Path

from .config import data_dir

MUSIC_DIR = Path(__file__).resolve().parent / "assets" / "music"
MOODS = ("lofi", "epico", "tranquilo", "energico")
USER_MOOD = "usuario"
_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
_FADE_OUT = 1.5


def _title(stem: str) -> str:
    return stem.replace("-", " ").replace("_", " ").title()


def list_music() -> list[dict]:
    """Bundled tracks (assets/music/<mood>/*) followed by user tracks (data_dir()/musica/*)."""
    tracks: list[dict] = []
    for mood in MOODS:
        for p in sorted((MUSIC_DIR / mood).glob("*")):
            if p.suffix.lower() in _EXTENSIONS:
                tracks.append({"id": f"{mood}/{p.stem}", "mood": mood, "title": _title(p.stem),
                               "path": p, "bundled": True})
    user_dir = data_dir() / "musica"
    if user_dir.is_dir():
        for p in sorted(user_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in _EXTENSIONS:
                tracks.append({"id": f"{USER_MOOD}/{p.stem}", "mood": USER_MOOD, "title": p.stem,
                               "path": p, "bundled": False})
    return tracks


def pick_track(choice: str, rng: random.Random | None = None) -> Path | None:
    """Profile `music` value -> file: "none" -> None, a mood -> random bundled track, else a path."""
    if not choice or choice == "none":
        return None
    if choice in MOODS:
        candidates = [t["path"] for t in list_music() if t["mood"] == choice]
        return (rng or random).choice(candidates) if candidates else None
    return Path(choice)


def music_input_args(path: Path) -> list[str]:
    """Input args for the music file; loops it forever (mix_filter trims it to the video length)."""
    return ["-stream_loop", "-1", "-i", str(path)]


def mix_filter(has_music: bool, volume: float, duration: float) -> str:
    """The audio `-filter_complex` graph; output label [aout].

    Input order the caller must use: input 1 = voice-over, input 2 = music (via music_input_args);
    input 0 is the video. With has_music=False only input 1 is used.
    The voice is normalised to -14 LUFS, resampled to 48 kHz (loudnorm outputs 192 kHz) and
    padded/trimmed to `duration`, so the output is always exactly `duration` long.
    The music is trimmed to `duration`, scaled by `volume`, faded out over the last 1.5 s, ducked
    by a sidechain keyed on the voice, mixed and limited (sample ceiling 0.75 keeps true peak
    <= -1 dBTP even when heavily limited). Only the music fades: the voice keeps its level to the
    very end, so the closing words are never swallowed (ruling R27).
    """
    # pad (or trim) the voice to exactly `duration`: a shorter voice would otherwise end the
    # sidechain/amix early and cut the music and its fade-out
    voice = (f"[1:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
             f"apad=whole_dur={duration:g},atrim=0:{duration:g}")
    if not has_music:
        return f"{voice}[aout]"
    fade_start = max(0.0, duration - _FADE_OUT)
    return ";".join([
        f"{voice},asplit=2[v][sc]",
        f"[2:a]aresample=48000,atrim=0:{duration:g},asetpts=PTS-STARTPTS,volume={volume},"
        f"afade=t=out:st={fade_start:g}:d={_FADE_OUT}[m]",
        "[m][sc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=300[md]",
        "[v][md]amix=inputs=2:normalize=0,alimiter=limit=0.75:level=disabled[aout]",
    ])
