from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from . import audio, captions, ffmpeg, llm, timeline, tts
from .config import data_dir, slugify
from .models import JobCancelled, Media, Profile, ProgressFn, RenderOptions, Script, UserError
from .render import render_video
from .stock import cache
from .stock.select import find_media

STEPS = ("script", "voice", "clips", "edit", "captions", "render")
_TOTAL = len(STEPS)
_DIMS = {"9:16": (1080, 1920), "16:9": (1920, 1080)}


@dataclass
class Providers:
    """Replaceable network-facing steps (the offline self-test injects generated ones).

    A field left as None means the real implementation, looked up when the job runs."""
    generate_script: Callable | None = None
    synthesize: Callable | None = None
    find_media: Callable | None = None
    download: Callable | None = None


def _create_output_dir(settings: dict, topic: str) -> Path:
    base = Path(settings["output_dir"]) if settings.get("output_dir") else data_dir() / "salidas"
    base.mkdir(parents=True, exist_ok=True)
    stem = f"{date.today().isoformat()}_{slugify(topic)}"
    n = 1
    while True:
        folder = base / (stem if n == 1 else f"{stem}-{n}")
        try:
            folder.mkdir(exist_ok=False)  # atomic: two jobs can't claim the same folder
            return folder
        except FileExistsError:
            n += 1


def _hook_word_count(script: Script) -> int:
    first = script.scenes[0].text.strip() if script.scenes else ""
    hook = script.hook.strip()
    if hook and first.startswith(hook):
        return len(hook.split())
    for i, ch in enumerate(first):
        if ch in ".?!…":
            return len(first[: i + 1].split())
    return len(first.split())


def _write_texts(folder: Path, script: Script) -> None:
    paragraphs = [script.title, script.hook] + [s.text for s in script.scenes]
    (folder / "guion.txt").write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in script.hashtags)
    (folder / "publicacion.txt").write_text(
        f"{script.title}\n\n{script.description}\n\n{tags}\n", encoding="utf-8")


def run_job(topic: str, profile: Profile, settings: dict, script: Script | None,
            cancel: threading.Event, progress: ProgressFn, providers: Providers | None = None) -> Path:
    """Run the six steps; returns the output folder. Failed or cancelled jobs leave nothing behind."""
    folder = _create_output_dir(settings, topic)
    try:
        workdir = folder / ".work"
        workdir.mkdir()
        _run(topic, profile, settings, script, cancel, progress, folder, workdir, providers or Providers())
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    shutil.rmtree(workdir, ignore_errors=True)
    return folder


def _run(topic: str, profile: Profile, settings: dict, script: Script | None,
         cancel: threading.Event, progress: ProgressFn, folder: Path, workdir: Path,
         providers: Providers) -> None:
    def begin(i: int) -> None:
        if cancel.is_set():
            raise JobCancelled()
        progress(STEPS[i - 1], i, _TOTAL, 0.0)

    def end(i: int) -> None:
        progress(STEPS[i - 1], i, _TOTAL, 100.0)

    width, height = _DIMS[profile.aspect]

    begin(1)
    if script is None:
        script = (providers.generate_script or llm.generate_script)(topic, profile, settings)
    end(1)

    begin(2)
    voice = (providers.synthesize or tts.synthesize)(script, profile, settings, workdir)
    end(2)

    begin(3)
    cuts = timeline.plan_cuts(voice.scene_bounds, voice.words)
    by_scene: dict[int, list[tuple[int, float, float]]] = {}
    for cut in cuts:
        by_scene.setdefault(cut[0], []).append(cut)
    used: set[str] = set()  # shared across scenes: no clip repeats in the video
    media_by_scene: dict[int, list[Media]] = {}
    for n, (idx, scene_cuts) in enumerate(sorted(by_scene.items())):
        if cancel.is_set():
            raise JobCancelled()
        progress("clips", 3, _TOTAL, 100.0 * n / len(by_scene))
        longest = max(e - s for _, s, e in scene_cuts)
        found = (providers.find_media or find_media)(script.scenes[idx].keywords, len(scene_cuts), longest, width, height, used, settings)
        for m in found:
            used.add(m.id)
            m.path = (providers.download or cache.download)(m)
        media_by_scene[idx] = found
    end(3)

    begin(4)
    try:
        shots = timeline.assign_media(cuts, media_by_scene)
    except ValueError:
        raise UserError("No se encontraron suficientes clips para el vídeo",
                        "Prueba otro tema o añade una clave de Pixabay en Ajustes") from None
    end(4)

    begin(5)
    ass = workdir / "subs.ass"
    captions.write_ass(voice.words, profile.subtitle, _hook_word_count(script), width, height,
                       ass, captions.FONTS_DIR)
    end(5)

    begin(6)
    opts = RenderOptions(width, height, profile.fps, ffmpeg.pick_encoder())
    render_video(shots, voice, ass, audio.pick_track(profile.music), profile.music_volume, opts,
                 workdir, folder / "video.mp4", cancel,
                 lambda pct: progress("render", 6, _TOTAL, pct * 100))
    _write_texts(folder, script)
    end(6)
