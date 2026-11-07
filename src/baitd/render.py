"""Renderer: shots -> smooth slow-zoom clips -> concat -> captions + audio mix in one final pass."""
from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Callable

from . import audio, captions
from .ffmpeg import run
from .models import JobCancelled, RenderOptions, Shot, Voiceover

_SHOT_WEIGHT = 0.6  # share of the progress bar used by the per-shot pass
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

_ENCODER_ARGS = {
    "libx264": ["-c:v", "libx264", "-crf", "18", "-preset", "medium"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-preset", "p5"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "20"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp", "-qp_i", "19", "-qp_p", "21"],
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-q:v", "65"],
}


def encoder_args(encoder: str) -> list[str]:
    """Video encoder arguments for the final pass (quality ~ CRF 18), always yuv420p."""
    return [*_ENCODER_ARGS.get(encoder, _ENCODER_ARGS["libx264"]), "-pix_fmt", "yuv420p"]


def _time_of(line: str) -> float | None:
    m = _TIME_RE.search(line)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def _shot_filter(w: int, h: int, fps: int, frames: int, zoom_from: float, zoom_to: float) -> str:
    """Cover-crop with a smooth, sub-pixel zoom.

    ffmpeg autorotates on decode, so cover/crop is computed from iw/ih inside the graph. The frame
    is cover-cropped to the largest zoom (P = zmax x output) and `perspective` (eval=frame) then
    samples a centred region of fractional size P/z with cubic interpolation, so the zoom moves
    in sub-pixel steps instead of the 1-2 px stair-steps of integer crops / even-sized scales.
    A final scale brings P back to w x h.
    """
    zmax = max(zoom_from, zoom_to)
    pw, ph = _even(w * zmax), _even(h * zmax)
    z = f"({zoom_from:.6f}+({zoom_to - zoom_from:.6f})*min(in/{max(frames - 1, 1)},1))"
    half_x, half_y = f"(W/2/{z})", f"(H/2/{z})"
    persp = (f"perspective=x0='W/2-{half_x}':y0='H/2-{half_y}':x1='W/2+{half_x}':y1='H/2-{half_y}'"
             f":x2='W/2-{half_x}':y2='H/2+{half_y}':x3='W/2+{half_x}':y3='H/2+{half_y}'"
             ":eval=frame:interpolation=cubic")
    return ",".join([
        f"fps={fps}",
        "setpts=PTS-STARTPTS",
        f"scale={pw}:{ph}:force_original_aspect_ratio=increase:flags=bicubic",
        f"crop={pw}:{ph}",
        persp,
        f"scale={w}:{h}:flags=bicubic",
        "setsar=1",
        "format=yuv420p",
    ])


def _rel(path: Path, workdir: Path) -> str:
    """Relative-to-workdir string when inside it (no escaping trouble), else absolute."""
    try:
        return Path(path).resolve().relative_to(workdir.resolve()).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def render_video(
    shots: list[Shot],
    voice: Voiceover,
    ass_path: Path,
    music: Path | None,
    music_volume: float,
    opts: RenderOptions,
    workdir: Path,
    out_path: Path,
    cancel: threading.Event,
    on_progress: Callable[[float], None],
) -> Path:
    workdir = Path(workdir)
    out_path = Path(out_path)
    workdir.mkdir(parents=True, exist_ok=True)
    if not shots:
        raise ValueError("render_video needs at least one shot")
    for s in shots:
        if s.media.path is None or not Path(s.media.path).exists():
            raise ValueError(f"Media {s.media.id} has no downloaded file")

    shot_files: list[Path] = []
    list_file = workdir / "shots.txt"
    joined = workdir / "joined.mp4"
    try:
        fonts = workdir / "fonts"
        fonts.mkdir(exist_ok=True)
        for ttf in captions.FONTS_DIR.glob("*.ttf"):
            if not (fonts / ttf.name).exists():
                shutil.copy2(ttf, fonts / ttf.name)
        ass_path = Path(ass_path)
        if ass_path.resolve() != (workdir / "subs.ass").resolve():
            shutil.copy2(ass_path, workdir / "subs.ass")

        def progress(f: float) -> None:
            on_progress(max(0.0, min(1.0, f)))

        progress(0.0)

        # frame counts from cumulative boundaries: no drift across shots
        fps = opts.fps
        counts = [max(1, round(s.end * fps) - round(s.start * fps)) for s in shots]
        total_frames = sum(counts)
        done_frames = 0
        for i, (shot, n) in enumerate(zip(shots, counts)):
            if cancel.is_set():
                raise JobCancelled()
            length = n / fps
            name = f"shot_{i:03d}.mp4"
            shot_files.append(workdir / name)
            if shot.media.kind == "photo":
                src = ["-loop", "1", "-framerate", str(fps)]
            else:
                src = ["-stream_loop", "-1"]
            base = done_frames

            def on_line(line: str, base=base, length=length, n=n) -> None:
                t = _time_of(line)
                if t is not None:
                    progress(_SHOT_WEIGHT * (base + min(t / length, 1.0) * n) / total_frames)

            run(
                [*src, "-i", _rel(shot.media.path, workdir), "-an",
                 "-vf", _shot_filter(opts.width, opts.height, fps, n, shot.zoom_from, shot.zoom_to),
                 "-frames:v", str(n), "-r", str(fps),
                 "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-video_track_timescale", "90000", name],
                cwd=workdir, cancel=cancel, on_line=on_line,
            )
            done_frames += n
            progress(_SHOT_WEIGHT * done_frames / total_frames)

        list_file.write_text("".join(f"file '{p.name}'\n" for p in shot_files), encoding="utf-8")
        run(["-f", "concat", "-safe", "0", "-i", list_file.name, "-c", "copy", joined.name],
            cwd=workdir, cancel=cancel)

        duration = voice.duration
        fc = (f"[0:v]ass=subs.ass:fontsdir=fonts[vout];"
              f"{audio.mix_filter(music is not None, music_volume, duration)}")
        args = ["-i", joined.name, "-i", _rel(voice.path, workdir)]
        if music is not None:
            args += audio.music_input_args(Path(_rel(music, workdir)))

        def on_final(line: str) -> None:
            t = _time_of(line)
            if t is not None:
                progress(_SHOT_WEIGHT + (1 - _SHOT_WEIGHT) * min(t / duration, 1.0))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        run([*args, "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]", "-shortest",
             *encoder_args(opts.encoder), "-r", str(fps),
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
             str(out_path.resolve())],
            cwd=workdir, cancel=cancel, on_line=on_final)
        progress(1.0)
        return out_path
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    finally:
        for p in [*shot_files, *workdir.glob("shot_*.mp4"), list_file, joined]:
            p.unlink(missing_ok=True)
