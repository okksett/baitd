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
SAMPLE_FPS = 8.0  # 4/s was enough to follow a face; telling who talks needs 8 (measured on a 2 min podcast)
DETECT_W = 640
SCENE_THRESHOLD = 0.35
DEAD_ZONE = 0.08  # of the crop width, each side
EMA_ALPHA = 0.15  # smoothing per EMA_STEP_S of time, whatever the sample rate
EMA_STEP_S = 0.25
IOU_MIN = 0.3
# Following the speaker when a shot holds two or more people. Each face carries an aligned crop of its
# mouth; the face whose mouth changes most between samples during a sentence is the one talking.
MOUTH_W, MOUTH_H = 32, 20
MAX_GAP_S = 0.3           # consecutive samples further apart than this are not compared
SPEAKER_MIN_SHARE = 0.5   # a face must be in half of a shot's samples to count as a speaker ...
SPEAKER_MIN_AREA = 0.25   # ... and at least a quarter of the biggest face's area
SWITCH_RATIO = 1.15       # the talker's mouth must move 15% more than anyone else's
MIN_TURN_S = 1.0          # shorter sentences ("Sí.", "No.") never move the crop
MIN_HOLD_S = 2.0          # stay on a speaker at least this long
MAX_KEYS = 200
SHARPEN = "unsharp=5:5:0.5:5:5:0.0"
MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"

Face = tuple  # x, y, w, h, score (source pixels)[, mouth crop: MOUTH_H x MOUTH_W float array or None]


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


def _mouth_crop(gray, row):
    """The mouth of one YuNet detection, aligned on its mouth corners (landmarks 10-13) so head motion
    mostly cancels out, z-normalised against lighting; None when it falls outside the frame."""
    import cv2
    import numpy as np

    rx, ry, lx, ly = (float(v) for v in row[10:14])
    cx, cy = (rx + lx) / 2, (ry + ly) / 2
    mw = max(4.0, ((lx - rx) ** 2 + (ly - ry) ** 2) ** 0.5)
    x0, x1 = int(cx - 0.8 * mw), int(cx + 0.8 * mw)
    y0, y1 = int(cy - 0.45 * mw), int(cy + 0.75 * mw)
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0] or x1 - x0 < 4 or y1 - y0 < 4:
        return None
    p = cv2.resize(gray[y0:y1, x0:x1], (MOUTH_W, MOUTH_H), interpolation=cv2.INTER_AREA).astype(np.float32)
    return (p - p.mean()) / (p.std() + 1e-3)


def detect_faces(src: SourceInfo, start: float, end: float, fps: float, cancel) -> list[tuple[float, list[Face]]]:
    """Faces at `fps` samples/s, as (clip-relative time, [(x, y, w, h, score, mouth crop)]) in source pixels.

    Frames come from an FFmpeg stdout pipe, one frame's bytes at a time; the process is killed on
    cancel or error."""
    import cv2
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
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if found is not None and len(found) else None
                for row in (found if found is not None else []):
                    x, y, w, h = (float(v) * factor for v in row[:4])
                    x0, y0 = max(0, int(x)), max(0, int(y))
                    w, h = min(int(w), src.width - x0), min(int(h), src.height - y0)
                    if w > 0 and h > 0 and x0 < src.width and y0 < src.height:
                        faces.append((x0, y0, w, h, float(row[-1]), _mouth_crop(gray, row)))
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
    """The best face track as {time: face}. Best = highest sum of area x centrality; the choice is
    made once per shot, so it cannot flip."""
    tracks = _tracks(samples, src_w)
    return max(tracks, key=lambda tr: tr["score"])["hits"] if tracks else {}


def _tracks(samples: list[tuple[float, list[Face]]], src_w: int) -> list[dict]:
    """Faces linked across samples by IoU: [{"last": Face, "hits": {t: Face}, "score": float}]."""
    tracks: list[dict] = []
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
    return tracks


def _mouth_activity(hits: dict[float, Face]) -> list[tuple[float, float]]:
    """(time, mean absolute change of the mouth crop since the previous sample) for one track."""
    import numpy as np

    out: list[tuple[float, float]] = []
    prev_t, prev = None, None
    for t, f in sorted(hits.items()):
        crop = f[5] if len(f) > 5 else None
        if crop is not None and prev is not None and t - prev_t <= MAX_GAP_S:
            out.append((t, float(np.abs(crop - prev).mean())))
        prev_t, prev = t, crop
    return out


def _speaker_segments(shot, b0: float, b1: float, talk: list[tuple[float, float]], src_w: int):
    """Split one shot by who is talking: [(t0, t1, {t: face} of the speaker)], or None when the shot
    does not hold two clear people or their mouths never say who talks (then the main face is used).
    The crop moves only for a sentence of MIN_TURN_S or more in which one mouth moves SWITCH_RATIO more
    than every other: at the sentence start, or MIN_HOLD_S after the last
    move if that comes later and still leaves MIN_TURN_S of the sentence."""
    import statistics

    tracks = _tracks(shot, src_w)
    if len(tracks) < 2:
        return None

    def area(tr):
        return statistics.mean(f[2] * f[3] for f in tr["hits"].values())

    biggest = max(area(tr) for tr in tracks)
    people = [tr for tr in tracks
              if len(tr["hits"]) >= SPEAKER_MIN_SHARE * len(shot) and area(tr) >= SPEAKER_MIN_AREA * biggest]
    if len(people) < 2:
        return None
    acts = [_mouth_activity(tr["hits"]) for tr in people]
    if any(len(a) < 8 for a in acts):
        return None
    # Raw change, not each face against its own baseline: the crops are already normalised for light and
    # contrast, and on a real podcast the raw score chose like a 20th-percentile baseline did, while a
    # baseline blows up the noise of a still mouth and fails when one person talks the whole shot.
    switches: list[tuple[float, int]] = []  # (time, index into people)
    for s0, s1 in talk:
        s0, s1 = max(s0, b0), min(s1, b1)
        if s1 - s0 < MIN_TURN_S:
            continue
        scores = []
        for a in acts:
            vals = [v for t, v in a if s0 <= t <= s1]
            scores.append(statistics.mean(vals) if vals else 0.0)
        order = sorted(range(len(people)), key=lambda k: -scores[k])
        best = order[0]
        if scores[best] <= 0 or scores[best] < SWITCH_RATIO * scores[order[1]]:
            continue
        if not switches:
            switches.append((b0, best))  # the first clear turn also decides who opens the shot
        elif best != switches[-1][1]:
            at = max(s0, switches[-1][0] + MIN_HOLD_S)  # too soon after the last move: move later
            if s1 - at >= MIN_TURN_S:
                switches.append((at, best))
    if not switches:
        return None
    segments = []
    for i, (t0, k) in enumerate(switches):
        t1 = switches[i + 1][0] if i + 1 < len(switches) else b1
        segments.append((t0, t1, {t: f for t, f in people[k]["hits"].items() if t0 <= t < t1}))
    return segments


def _clamp(x: float, hi: int) -> int:
    return int(round(min(max(x, 0.0), float(hi))))


def _plan_keys(samples, cuts: list[float], duration: float, src_w: int, crop_w: int,
               talk: list[tuple[float, float]] | None = None) -> list[tuple[float, int]]:
    """Crop x keys. Each shot (between scene cuts) follows one face, or, with `talk` (clip-relative
    sentence spans), whoever is talking; every change of shot or speaker is a jump, not a slide."""
    hi = src_w - crop_w
    centre = hi // 2
    bounds = [0.0, *sorted(c for c in cuts if 0.0 < c < duration), float("inf")]
    segments: list[tuple[float, dict[float, Face]]] = []  # (start, the face to follow)
    for i in range(len(bounds) - 1):
        b0, b1 = bounds[i], bounds[i + 1]
        shot = [(t, fs) for t, fs in samples if b0 <= t < b1]
        split = _speaker_segments(shot, b0, b1, talk, src_w) if talk else None
        if split is None:
            segments.append((b0, _main_track(shot, src_w)))
        else:
            segments.extend((t0, track) for t0, _, track in split)

    def target(face: Face) -> float:
        return min(max(face[0] + face[2] / 2 - crop_w / 2, 0.0), float(hi))

    keys: list[tuple[float, int]] = []
    prev_x = None
    for b0, track in segments:
        hits = sorted(track.items())
        if hits:
            cur = target(hits[0][1])
            seg = [(b0, _clamp(cur, hi))]
            last_t = hits[0][0]
            for t, face in hits[1:]:
                tgt = target(face)
                if abs(tgt - cur) > DEAD_ZONE * crop_w:
                    alpha = 1.0 - (1.0 - EMA_ALPHA) ** ((t - last_t) / EMA_STEP_S)
                    cur += alpha * (tgt - cur)
                last_t = t
                seg.append((t, _clamp(cur, hi)))
        else:
            seg = [(b0, centre if prev_x is None else prev_x)]
        if keys and keys[-1][1] != seg[0][1]:
            keys.append((b0, keys[-1][1]))  # old value up to the cut ...
            keys.append(seg[0])             # ... new value from it: a jump, not a slide
        elif not keys:
            keys.append(seg[0])
        keys.extend(seg[1:])
        prev_x = keys[-1][1]
    return keys


# ---------------------------------------------------------------- plan

def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def plan_crop(src: SourceInfo, start: float, end: float, out_w: int, out_h: int, mode: str, cancel,
              faces=None, cuts=None, talk: list[tuple[float, float]] | None = None) -> CropPlan:
    """`talk`: clip-relative (start, end) of each sentence; with it a shot of two people follows the speaker."""
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
        keys = _plan_keys(faces, cuts, end - start, src.width, crop_w, talk)
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
                f"crop={out_w}:{out_h},boxblur=20:2[bg];"
                f"[b]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
    keys = plan.keys if plan.mode == "cara" else [plan.keys[0]]
    chain = f"crop={plan.crop_w}:{plan.crop_h}:x='{_x_expr(keys)}':y=(ih-{plan.crop_h})/2,scale={out_w}:{out_h}:flags=lanczos"
    return chain + ("," + SHARPEN if plan.sharpen else "")
