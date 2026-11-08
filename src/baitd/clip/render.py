"""Render one clip: silence cuts, crop, captions, hook title, credit and loudness in two FFmpeg passes
(a WAV of the cut audio, then the video pass that mixes it)."""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .. import audio, captions
from ..ffmpeg import JobCancelled, encoder_args, probe, run
from ..models import Profile, RenderOptions, UserError, Word
from ..render import _COLOR, _rel, _time_of
from .framing import CropPlan, crop_filter
from .models import Moment, SourceInfo

SILENCE_GAP = 0.6
SILENCE_PAD = 0.15  # silence kept around speech at every cut and at both ends of the clip
_AUDIO_WEIGHT = 0.1  # share of the progress bar used by the audio pass
_WAVE_BG = 0.25  # audiogram background = highlight colour at 25 % brightness
_SEEK_LEAD = 1.0  # seconds decoded before the clip start (keyframe slack)


def keep_segments(words: list[Word], start: float, end: float, gap: float = SILENCE_GAP) -> list[tuple[float, float]]:
    """Absolute (start, end) pieces of [start, end) to keep: speech with 0.15 s of silence around it.
    A gap longer than `gap` between words is cut; the silence before the first and after the last
    word is trimmed to 0.15 s."""
    inside = [w for w in words if w.end > start and w.start < end]
    if not inside:
        return [(start, end)]
    segs: list[list[float]] = [[inside[0].start, inside[0].end]]
    for w in inside[1:]:
        if w.start - segs[-1][1] > gap:
            segs.append([w.start, w.end])
        else:
            segs[-1][1] = max(segs[-1][1], w.end)
    return [(max(start, a - SILENCE_PAD), min(end, b + SILENCE_PAD)) for a, b in segs]


def remap_words(words: list[Word], segments: list[tuple[float, float]]) -> list[Word]:
    """Words with clip-relative times on the post-cut timeline; words outside every segment are dropped."""
    out: list[Word] = []
    offset = 0.0
    for a, b in segments:
        for w in words:
            if a <= w.start < b:
                s = offset + (w.start - a)
                out.append(Word(w.text, s, offset + min(w.end, b) - a))
        offset += b - a
    return out


def clip_fps(requested: int, source_fps: float) -> int:
    """60 only when asked for and the source really has >= 50 fps, else 30."""
    return 60 if requested >= 60 and source_fps >= 50 else 30


# ---------------------------------------------------------------- crop keys on the post-cut timeline

def _x_at(keys: list[tuple[float, int]], t: float, after: bool) -> float:
    """x of a piecewise-linear key list at t; at a jump, `after` picks the value after it."""
    prev = [k for k in keys if (k[0] <= t if after else k[0] < t)]
    nxt = [k for k in keys if (k[0] > t if after else k[0] >= t)]
    if not prev:
        return float(keys[0][1])
    if not nxt:
        return float(prev[-1][1])
    (t0, x0), (t1, x1) = prev[-1], nxt[0]
    return x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else float(x1 if after else x0)


def remap_keys(keys: list[tuple[float, int]], segments: list[tuple[float, float]], span_start: float
               ) -> list[tuple[float, int]]:
    """Span-relative crop keys -> post-cut timeline. Keys in removed gaps are dropped, every segment
    boundary gets a key with the interpolated x, jump pairs are kept."""
    keys = sorted(keys, key=lambda k: k[0])
    out: list[tuple[float, int]] = []
    offset = 0.0
    for a, b in segments:
        lo, hi = a - span_start, b - span_start
        out.append((offset, round(_x_at(keys, lo, True))))
        out.extend((offset + k[0] - lo, k[1]) for k in keys if lo < k[0] < hi)
        offset += b - a
        out.append((offset, round(_x_at(keys, hi, False))))
    if len({x for _, x in out}) == 1:
        return [(0.0, out[0][1])]
    return out


# ---------------------------------------------------------------- render

def _dark(color: str, k: float = _WAVE_BG) -> str:
    h = color.lstrip("#")
    r, g, b = (int(int(h[i:i + 2], 16) * k) for i in (0, 2, 4))
    return f"0x{r:02X}{g:02X}{b:02X}"


def _trim_graph(segs: list[tuple[float, float]], seek: float, kind: str) -> str:
    """trim/atrim every segment (times relative to the seek point) and concat them to [cat]."""
    pre, label = ("", "v") if kind == "v" else ("a", "a")
    parts = []
    for i, (a, b) in enumerate(segs):
        parts.append(f"[0:{label}]{pre}trim=start={a - seek:.3f}:end={b - seek:.3f},{pre}setpts=PTS-STARTPTS[{kind}{i}]")
    joined = "".join(f"[{kind}{i}]" for i in range(len(segs)))
    flag = "v=1:a=0" if kind == "v" else "v=0:a=1"
    parts.append(f"{joined}concat=n={len(segs)}:{flag}[cat]")
    return ";".join(parts)


def render_clip(src: SourceInfo, m: Moment, words: list[Word], plan: CropPlan | None, profile: Profile,
                opts: RenderOptions, workdir: Path, out_path: Path, cancel, on_progress: Callable[[float], None]
                ) -> Path:
    """Render moment `m` of `src` to `out_path` (1080x1920 by default, CFR, H.264 + AAC, -14 LUFS).

    `plan` is the crop plan (None for an audio-only source: audiogram on a coloured background).
    `words` are absolute source times."""
    workdir, out_path = Path(workdir), Path(out_path)
    workdir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    clipping = profile.clipping
    wav, ass_name, fonts = workdir / "clip_audio.wav", "subs.ass", workdir / "fonts"
    try:
        def progress(f: float) -> None:
            on_progress(max(0.0, min(1.0, f)))

        progress(0.0)
        segs = keep_segments(words, m.start, m.end)
        duration = sum(b - a for a, b in segs)
        seek = max(0.0, m.start - _SEEK_LEAD)
        fps = opts.fps
        w, h = opts.width, opts.height
        src_arg = str(Path(src.path).resolve())

        # pass 1: the cut audio as a WAV
        run(["-ss", f"{seek:.3f}", "-i", src_arg, "-filter_complex", _trim_graph(segs, seek, "a"),
             "-map", "[cat]", "-vn", "-ar", "48000", "-ac", "2", wav.name],
            cwd=workdir, cancel=cancel,
            on_line=lambda ln: (lambda t: progress(_AUDIO_WEIGHT * min(t / duration, 1.0)) if t is not None else None)(_time_of(ln)))

        # captions
        fonts.mkdir(exist_ok=True)
        for ttf in captions.FONTS_DIR.glob("*.ttf"):
            if not (fonts / ttf.name).exists():
                shutil.copy2(ttf, fonts / ttf.name)
        style = profile.subtitle if plan is not None else replace(profile.subtitle, position="middle")
        credit = ""
        if clipping.credit:
            credit = m.credit or (f"Fuente: @{src.channel}" if src.channel else "")
        captions.write_clip_ass(
            remap_words(words, segs), style, w, h, workdir / ass_name, fonts,
            top_text=m.title if clipping.hook_title else "", top_until=3.0, credit=credit)

        # pass 2: video
        music = audio.pick_track(profile.music) if clipping.music else None
        norm = (f"scale={w}:{h}:flags=bicubic:in_range=auto:out_range=tv:out_color_matrix=bt709,"
                "setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709,setsar=1,format=yuv420p,"
                f"ass={ass_name}:fontsdir=fonts")
        args = ["-reinit_filter", "0"]
        if plan is not None:
            nplan = replace(plan, keys=remap_keys(plan.keys, segs, m.start)) if plan.mode == "cara" else plan
            args += ["-ss", f"{seek:.3f}", "-i", src_arg, "-i", wav.name]
            video = (f"{_trim_graph(segs, seek, 'v')};[cat]fps={fps}[cf];[cf]{crop_filter(nplan, w, h)}[cr];"
                     f"[cr]{norm}[vout]")
            wave_idx = None
        else:
            colour = _dark(profile.subtitle.highlight)
            args += ["-f", "lavfi", "-i", f"color=c={colour}:s={w}x{h}:r={fps}:d={duration:.3f}", "-i", wav.name]
        if music is not None:
            args += audio.music_input_args(Path(_rel(music, workdir)))
        if plan is None:
            wave_idx = 3 if music is not None else 2
            args += ["-i", wav.name]
            video = (f"[{wave_idx}:a]showwaves=s={w}x{h // 3}:mode=cline:rate={fps}:colors=white,format=rgba[wv];"
                     f"[0:v][wv]overlay=0:{h // 3},{norm}[vout]")
        fc = f"{video};{audio.mix_filter(music is not None, profile.music_volume, duration)}"

        def on_line(line: str) -> None:
            t = _time_of(line)
            if t is not None:
                progress(_AUDIO_WEIGHT + (1 - _AUDIO_WEIGHT) * min(t / duration, 1.0))

        def final(encoder: str) -> None:
            run([*args, "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]", "-t", f"{duration:.3f}",
                 *encoder_args(encoder), *_COLOR, "-r", str(fps),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
                 str(out_path.resolve())],
                cwd=workdir, cancel=cancel, on_line=on_line)

        try:
            final(opts.encoder)
        except RuntimeError:
            if opts.encoder == "libx264":
                raise
            out_path.unlink(missing_ok=True)
            final("libx264")  # the hardware encoder rejected its args: one retry in software
        if not probe(out_path)["width"]:  # never hand the user a clip without picture
            raise UserError("El clip salió sin imagen", "Prueba con otro archivo de video o audio")
        progress(1.0)
        return out_path
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    finally:
        wav.unlink(missing_ok=True)
        (workdir / ass_name).unlink(missing_ok=True)
        shutil.rmtree(fonts, ignore_errors=True)
