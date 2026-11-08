"""Where to crop a landscape source into a vertical clip: scene cuts, YuNet face tracking, crop plans.

All times in a CropPlan are relative to the clip's source span [start, end), before any silence cuts
(the renderer remaps them). `crop_filter` expressions use FFmpeg's `t`.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import ffmpeg
from ..ffmpeg import JobCancelled
from .models import SourceInfo

__all__ = ["MAX_UPSCALE", "CropPlan", "required_scale", "scene_cuts", "detect_faces", "plan_crop", "crop_filter"]

MAX_UPSCALE = 1.8
SHARPEN_ABOVE = 1.25
SCORE_MIN = 0.6
SAMPLE_FPS = 4.0
DETECT_W = 640
SCENE_THRESHOLD = 0.35
DEAD_ZONE = 0.08  # of the crop width, each side
EMA_ALPHA = 0.15
IOU_MIN = 0.3
MAX_KEYS = 200
SHARPEN = "unsharp=5:5:0.5:5:5:0.0"
MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"

Face = tuple[int, int, int, int, float]  # x, y, w, h, score (source pixels)


@dataclass
class CropPlan:
    mode: str  # "cara" | "centro" | "completo"
    crop_w: int
    crop_h: int  # source pixels ("completo": the source size)
    keys: list[tuple[float, int]]  # (clip-relative time, crop x), piecewise-linear; two keys at one time = a jump
    sharpen: bool  # scale > 1.25


def _window(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[float, float]:
    """Largest window with the output aspect that fits inside the source."""
    if src_w * out_h >= out_w * src_h:  # source is wider than the output aspect
        return src_h * out_w / out_h, float(src_h)
    return float(src_w), src_w * out_h / out_w


def required_scale(src_w: int, src_h: int, out_w: int, out_h: int) -> float:
    cw, ch = _window(src_w, src_h, out_w, out_h)
    return max(out_w / cw, out_h / ch)


# ---------------------------------------------------------------- scene cuts

def scene_cuts(src: SourceInfo, start: float, end: float, cancel) -> list[float]:
    """Clip-relative times of hard cuts in [start, end)."""
    err = ffmpeg.run(
        ["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src.path),
         "-vf", f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo", "-f", "null", "-"],
        cancel=cancel,
    )
    cuts = [float(m) for m in re.findall(r"pts_time:(\d+(?:\.\d+)?)", err)]
    return sorted({round(c, 3) for c in cuts if 0.0 < c < end - start})


# ---------------------------------------------------------------- face detection

def _make_detector(size: tuple[int, int]):
    import cv2
    import numpy as np
    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)  # silence dnn backend notices
    except AttributeError:
        pass
    # Load from a buffer, not a path: OpenCV cannot open non-ASCII paths on Windows.
    model = np.frombuffer(MODEL_PATH.read_bytes(), dtype=np.uint8)
    return cv2.FaceDetectorYN.create("onnx", model, b"", size, SCORE_MIN, 0.3, 5000)


def detect_faces(src: SourceInfo, start: float, end: float, fps: float, cancel) -> list[tuple[float, list[Face]]]:
    """Faces at `fps` samples/s, as (clip-relative time, [(x, y, w, h, score)]) in source pixels.

    Frames come from an FFmpeg stdout pipe, one frame's bytes at a time; the process is killed on
    cancel or error."""
    import numpy as np

    fw = DETECT_W
    fh = max(2, int(round(src.height * fw / src.width / 2)) * 2)
    fsize = fw * fh * 3
    factor = src.width / fw
    detector = _make_detector((fw, fh))
    cmd = [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src.path),
           "-vf", f"fps={fps:g},scale={fw}:{fh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    out: list[tuple[float, list[Face]]] = []
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            i = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise JobCancelled()
                buf = proc.stdout.read(fsize)
                if len(buf) < fsize:
                    break
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(fh, fw, 3)
                _, found = detector.detect(frame)
                faces: list[Face] = []
                for row in (found if found is not None else []):
                    x, y, w, h = (float(v) * factor for v in row[:4])
                    x0, y0 = max(0, int(x)), max(0, int(y))
                    w, h = min(int(w), src.width - x0), min(int(h), src.height - y0)
                    if w > 0 and h > 0 and x0 < src.width and y0 < src.height:
                        faces.append((x0, y0, w, h, float(row[-1])))
                out.append((i / fps, faces))
                i += 1
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        proc.wait()
        if proc.returncode != 0:
            errf.seek(0)
            tail = errf.read().decode("utf-8", errors="replace")[-800:]
            raise RuntimeError("ffmpeg face-sampling failed (exit %d):\n%s" % (proc.returncode, tail))
    return out


# ---------------------------------------------------------------- tracking

def _iou(a: Face, b: Face) -> float:
    ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _main_track(samples: list[tuple[float, list[Face]]], src_w: int) -> dict[float, Face]:
    """Tracks faces across samples by IoU and returns the best track as {time: face}.
    Best = highest sum of area x centrality; the choice is made once per shot, so it cannot flip."""
    tracks: list[dict] = []  # {"last": Face, "hits": {t: Face}, "score": float}
    for t, faces in samples:
        taken: set[int] = set()
        for f in sorted(faces, key=lambda f: -f[2] * f[3]):
            best, best_iou = None, IOU_MIN
            for idx, tr in enumerate(tracks):
                if idx in taken:
                    continue
                v = _iou(tr["last"], f)
                if v >= best_iou:
                    best, best_iou = idx, v
            if best is None:
                tracks.append({"last": f, "hits": {}, "score": 0.0})
                best = len(tracks) - 1
            tr = tracks[best]
            taken.add(best)
            centrality = max(0.05, 1.0 - abs(f[0] + f[2] / 2 - src_w / 2) / (src_w / 2))
            tr["last"] = f
            tr["hits"][t] = f
            tr["score"] += f[2] * f[3] * centrality
    if not tracks:
        return {}
    return max(tracks, key=lambda tr: tr["score"])["hits"]


def _clamp(x: float, hi: int) -> int:
    return int(round(min(max(x, 0.0), float(hi))))


def _plan_keys(samples, cuts: list[float], duration: float, src_w: int, crop_w: int) -> list[tuple[float, int]]:
    hi = src_w - crop_w
    centre = hi // 2
    bounds = [0.0, *sorted(c for c in cuts if 0.0 < c < duration), float("inf")]
    keys: list[tuple[float, int]] = []
    prev_x = None
    for i in range(len(bounds) - 1):
        b0, b1 = bounds[i], bounds[i + 1]
        track = _main_track([(t, fs) for t, fs in samples if b0 <= t < b1], src_w)
        hits = sorted(track.items())
        if hits:
            def target(face: Face) -> float:
                return min(max(face[0] + face[2] / 2 - crop_w / 2, 0.0), float(hi))
            cur = target(hits[0][1])
            shot = [(b0, _clamp(cur, hi))]
            for t, face in hits[1:]:
                tgt = target(face)
                if abs(tgt - cur) > DEAD_ZONE * crop_w:
                    cur += EMA_ALPHA * (tgt - cur)
                shot.append((t, _clamp(cur, hi)))
        else:
            shot = [(b0, centre if prev_x is None else prev_x)]
        if keys and keys[-1][1] != shot[0][1]:
            keys.append((b0, keys[-1][1]))  # old value up to the cut ...
            keys.append(shot[0])            # ... new value from it: a jump, not a slide
        elif not keys:
            keys.append(shot[0])
        keys.extend(shot[1:])
        prev_x = keys[-1][1]
    return keys


# ---------------------------------------------------------------- plan

def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def plan_crop(src: SourceInfo, start: float, end: float, out_w: int, out_h: int, mode: str, cancel,
              faces=None, cuts=None) -> CropPlan:
    scale = required_scale(src.width, src.height, out_w, out_h)
    if scale > MAX_UPSCALE or mode == "completo":
        return CropPlan("completo", src.width, src.height, [(0.0, 0)], False)
    cw_f, ch_f = _window(src.width, src.height, out_w, out_h)
    crop_w, crop_h = min(_even(cw_f), src.width // 2 * 2), min(_even(ch_f), src.height // 2 * 2)
    sharpen = scale > SHARPEN_ABOVE
    centre = (src.width - crop_w) // 2
    if mode == "centro" or crop_w >= src.width:  # full-width window: nothing to follow
        return CropPlan("centro" if mode == "centro" else "cara", crop_w, crop_h, [(0.0, centre)], sharpen)
    if cuts is None:
        cuts = scene_cuts(src, start, end, cancel)
    if faces is None:
        faces = detect_faces(src, start, end, SAMPLE_FPS, cancel)
    if any(fs for _, fs in faces):
        keys = _plan_keys(faces, cuts, end - start, src.width, crop_w)
    else:
        keys = [(0.0, centre)]
    return CropPlan("cara", crop_w, crop_h, keys, sharpen)


# ---------------------------------------------------------------- filter

def _decimate(keys: list[tuple[float, int]]) -> list[tuple[float, int]]:
    """At most MAX_KEYS keys; the ends and both keys of every jump pair are always kept."""
    if len(keys) <= MAX_KEYS:
        return keys
    must = {0, len(keys) - 1}
    for i in range(1, len(keys)):
        if keys[i][0] == keys[i - 1][0]:
            must.update((i - 1, i))
    rest = [i for i in range(len(keys)) if i not in must]
    budget = max(0, MAX_KEYS - len(must))
    if budget and rest:
        step = len(rest) / budget
        must.update(rest[int(j * step)] for j in range(min(budget, len(rest))))
    return [keys[i] for i in sorted(must)]


def _x_expr(keys: list[tuple[float, int]]) -> str:
    """Piecewise-linear x(t). Two keys at one time make a jump. Uses commas, so it must be quoted
    in a filtergraph."""
    keys = sorted(_decimate(keys), key=lambda k: k[0])
    if len(keys) == 1:
        return str(keys[0][1])
    if keys[0][0] > 0:
        keys = [(0.0, keys[0][1]), *keys]
    # A sum of windowed segments (no nested if(): its depth hits FFmpeg's evaluator limit).
    terms = []
    for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
        t0, t1 = round(t0, 3), round(t1, 3)
        if t1 > t0:
            terms.append(f"gte(t,{t0:g})*lt(t,{t1:g})*({x0}+({x1 - x0})*(t-{t0:g})/{t1 - t0:.3f})")
    terms.append(f"gte(t,{round(keys[-1][0], 3):g})*{keys[-1][1]}")
    return _balanced_sum(terms)


def _balanced_sum(terms: list[str]) -> str:
    """Sum as a balanced tree of parentheses: FFmpeg's evaluator fails on expression trees that are
    ~100 levels deep, which a plain a+b+c+... of 200 terms is."""
    if len(terms) == 1:
        return terms[0]
    mid = len(terms) // 2
    return f"({_balanced_sum(terms[:mid])}+{_balanced_sum(terms[mid:])})"


def crop_filter(plan: CropPlan, out_w: int, out_h: int) -> str:
    """FFmpeg filter chain from the (single, unlabelled) video input to exactly out_w x out_h.

    Embed it as `[in]<chain>[out]` in a filtergraph, or pass it to -vf. It needs no pad labels of its
    own (the "completo" chain uses internal labels a, b, bg, fg). Key times are `t` of the stream the
    chain receives: remap them to that stream's timeline before use. The x expression is single-quoted
    because it contains commas.
    """
    if plan.mode == "completo":
        return (f"split[a][b];[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},boxblur=20:2[bg];[b]scale={out_w}:-2:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    keys = plan.keys if plan.mode == "cara" else [plan.keys[0]]
    chain = f"crop={plan.crop_w}:{plan.crop_h}:x='{_x_expr(keys)}':y=(ih-{plan.crop_h})/2,scale={out_w}:{out_h}:flags=lanczos"
    return chain + ("," + SHARPEN if plan.sharpen else "")
