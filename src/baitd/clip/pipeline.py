"""Clipping jobs for the queue: auto (link -> clips), analyze (link -> candidate moments on disk) and
export (chosen moments -> clips). Every network- or model-facing step can be replaced through
ClipProviders, so the self-test and the tests run offline."""
from __future__ import annotations

import json
import logging
import math
import re
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import config, ffmpeg
from ..models import JobCancelled, Profile, ProgressFn, RenderOptions, UserError, Word
from ..pipeline import _create_output_dir
from . import framing, sources, transcribe
from .models import FRAMINGS, ClipProfile, Moment, SourceInfo, Transcript
from .moments import find_moments
from .render import clip_fps, render_clip

__all__ = ["CLIP_STEPS", "CLIP_JOB_KINDS", "ClipProviders", "analyses_dir", "run_clip_auto", "run_clip_analyze",
           "run_clip_export", "load_analysis", "delete_analysis"]

log = logging.getLogger(__name__)

CLIP_STEPS = ("source", "transcribe", "moments", "framing", "render")
CLIP_JOB_KINDS = ("clip_auto", "clip_analyze", "clip_export")
_TOTAL = len(CLIP_STEPS)
_DIMS = {"9:16": (1080, 1920), "16:9": (1920, 1080)}
_ID = re.compile(r"[0-9a-f]{12}")
MIN_CLIP_S, MAX_CLIP_S = 10.0, 180.0  # clip length limits
_MAX_EXPORT = 20  # same cap as the clip count of a run


@dataclass
class ClipProviders:
    """Replaceable network- or model-facing steps; None means the real implementation."""
    fetch_url: Callable | None = None     # (url, cancel, on_progress) -> SourceInfo
    transcribe: Callable | None = None    # (audio, provider, language, settings, model, cancel, on_progress) -> Transcript
    complete: Callable | None = None      # (prompt) -> str
    detect_faces: Callable | None = None  # (src, start, end, fps, cancel) -> [(t, [faces])]


# ---------------------------------------------------------------- errors

def _original_gone() -> UserError:
    return UserError("El video original ya no está", "Vuelve a pegar el enlace o subir el archivo")


def _damaged() -> UserError:
    return UserError("El análisis está dañado", "Vuelve a buscar momentos")


def _bad_times() -> UserError:
    return UserError("Los tiempos de un clip no son válidos", "Revisa el inicio y el final de cada clip")


# ---------------------------------------------------------------- validation

def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("not a finite number")
    return float(v)


def _text(d: dict, key: str, default: str = "") -> str:
    v = d.get(key, default)
    if not isinstance(v, str):
        raise ValueError(f"{key} must be text")
    return v


def _moment_from_dict(d) -> Moment:
    """A Moment from untrusted data (a file on disk or the export payload); ValueError when malformed."""
    if not isinstance(d, dict):
        raise ValueError("moment must be an object")
    start, end = _num(d.get("start")), _num(d.get("end"))
    if not 0 <= start < end:
        raise ValueError("start must be before end")
    tags = d.get("hashtags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("hashtags must be a list of text")
    framing_ = d.get("framing", "cara")
    if framing_ not in FRAMINGS:
        raise ValueError("bad framing")
    return Moment(start=start, end=end, kind=_text(d, "kind", "interesante"),
                  score=round(_num(d.get("score", 0))), title=_text(d, "title"),
                  description=_text(d, "description"), hashtags=list(tags), text=_text(d, "text"),
                  framing=framing_, credit=_text(d, "credit"))


def _transcript_from_dict(d) -> Transcript:
    if not isinstance(d, dict) or not isinstance(d.get("words"), list) or not isinstance(d.get("language"), str):
        raise ValueError("bad transcript")
    words = []
    for w in d["words"]:
        if not isinstance(w, dict) or not isinstance(w.get("text"), str):
            raise ValueError("bad word")
        s, e = _num(w.get("start")), _num(w.get("end"))
        if s < 0 or e < s:
            raise ValueError("bad word times")
        words.append(Word(w["text"], s, e))
    duration = _num(d.get("duration"))
    if duration < 0:
        raise ValueError("bad duration")
    return Transcript(language=d["language"], words=words, duration=duration)


def _validate_analysis(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("analysis must be an object")
    if not isinstance(raw.get("source_id"), str) or not _ID.fullmatch(raw["source_id"]):
        raise ValueError("bad source id")
    ClipProfile.from_dict(raw.get("profile"))
    _transcript_from_dict(raw.get("transcript"))
    if not isinstance(raw.get("moments"), list):
        raise ValueError("moments must be a list")
    for m in raw["moments"]:
        _moment_from_dict(m)
    if not isinstance(raw.get("created_at"), str):
        raise ValueError("bad created_at")
    return raw


# ---------------------------------------------------------------- analyses on disk

def analyses_dir() -> Path:
    d = config.data_dir() / "analisis"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_id(analysis_id) -> bool:
    return isinstance(analysis_id, str) and bool(_ID.fullmatch(analysis_id))


def load_analysis(analysis_id: str) -> dict:
    """The validated analysis file. Missing (or a bad id): the original video is gone; malformed: damaged."""
    if not _valid_id(analysis_id):
        raise _original_gone()
    try:
        text = (analyses_dir() / f"{analysis_id}.json").read_text(encoding="utf-8")
    except OSError:
        raise _original_gone() from None
    try:
        return _validate_analysis(json.loads(text))
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise _damaged() from None


def delete_analysis(analysis_id: str) -> None:
    if _valid_id(analysis_id):
        try:
            (analyses_dir() / f"{analysis_id}.json").unlink(missing_ok=True)
        except OSError:
            log.warning("No se pudo borrar el análisis %s", analysis_id)


def _delete_analyses_of(source_id: str) -> None:
    """Analyses are useless without their source: remove every one that points at it."""
    for f in analyses_dir().glob("*.json"):
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("source_id") == source_id:
                delete_analysis(f.stem)
        except (OSError, ValueError, AttributeError):
            continue


# ---------------------------------------------------------------- shared pieces

class _Steps:
    def __init__(self, progress: ProgressFn, cancel: threading.Event):
        self._progress, self._cancel = progress, cancel

    def begin(self, i: int) -> None:
        if self._cancel.is_set():
            raise JobCancelled()
        self._progress(CLIP_STEPS[i - 1], i, _TOTAL, 0.0)

    def frac(self, i: int, f: float) -> None:
        self._progress(CLIP_STEPS[i - 1], i, _TOTAL, max(0.0, min(1.0, f)) * 100.0)

    def end(self, i: int) -> None:
        self._progress(CLIP_STEPS[i - 1], i, _TOTAL, 100.0)


def _payload(item: dict) -> dict:
    p = item.get("payload")
    return p if isinstance(p, dict) else {}


def _resolve(item: dict, with_overrides: bool = True) -> tuple[Profile, ClipProfile]:
    """The queued profile snapshot and the effective clipping options (profile + the job's overrides)."""
    try:
        profile = Profile.from_dict(item["profile"])
    except (TypeError, ValueError, KeyError, AttributeError):
        raise UserError("El perfil tiene valores no válidos", "Revisa los campos del formulario") from None
    overrides = _payload(item).get("overrides") or {} if with_overrides else {}
    try:
        if not isinstance(overrides, dict):
            raise ValueError("overrides must be an object")
        opts = ClipProfile.from_dict({**profile.clipping.to_dict(), **overrides})
    except ValueError:
        raise UserError("Ajustes de clipping no válidos", "Revisa número de clips y duración en el perfil") from None
    return profile, opts


def _acquire_source(item: dict, providers: ClipProviders, cancel: threading.Event,
                    on_progress: Callable[[float], None]) -> tuple[SourceInfo, bool]:
    """(source, fetched by this job). A source the user already has is never deleted on failure."""
    s = _payload(item).get("source")
    s = s if isinstance(s, dict) else {}
    if isinstance(s.get("url"), str) and s["url"].strip():
        sources.check_url(s["url"])
        return (providers.fetch_url or sources.fetch_url)(s["url"].strip(), cancel, on_progress), True
    if isinstance(s.get("source_id"), str):
        return sources.load_source(s["source_id"]), False
    raise UserError("Falta el video de origen", "Pega un enlace o sube un archivo")


def _understand(src: SourceInfo, profile: Profile, opts: ClipProfile, settings: dict, cancel: threading.Event,
                steps: _Steps, providers: ClipProviders, want: int) -> tuple[Transcript, list[Moment]]:
    """Steps 2 and 3: audio + transcript, then the ranked moments. Big files live in a temp folder."""
    with tempfile.TemporaryDirectory(prefix="baitd-clip-") as tmp:
        steps.begin(2)
        audio = sources.extract_audio(src, Path(tmp) / "audio.wav", cancel)
        language = None if opts.language == "auto" else opts.language
        transcript = (providers.transcribe or transcribe.transcribe)(
            audio, opts.transcriber, language, settings, opts.whisper_model, cancel,
            lambda f: steps.frac(2, f))
        steps.end(2)

        steps.begin(3)
        moments = find_moments(transcript, audio, opts, profile, settings, want, cancel,
                               lambda f: steps.frac(3, f), complete=providers.complete)
        steps.end(3)
    return transcript, moments


def _open_output(settings: dict, title: str, on_output: Callable[[Path], None] | None) -> Path:
    folder = _create_output_dir(settings, title)
    try:
        (folder / ".work").mkdir()
        if on_output is not None:
            on_output(folder)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder


def _hms(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _write_texts(folder: Path, moments: list[Moment]) -> None:
    blocks = []
    for i, m in enumerate(moments, 1):
        tags = " ".join(t if t.startswith("#") else f"#{t}" for t in m.hashtags)
        blocks.append(f"clip-{i:02d}.mp4 ({_hms(m.start)}–{_hms(m.end)})\n{m.title}\n\n{m.description}\n\n{tags}\n")
    (folder / "publicacion.txt").write_text("\n".join(blocks), encoding="utf-8")


def _render_all(src: SourceInfo, moments: list[Moment], words: list[Word], profile: Profile, opts: ClipProfile,
                folder: Path, cancel: threading.Event, steps: _Steps, providers: ClipProviders) -> None:
    """Steps 4 and 5: a crop plan for every moment, then one rendered clip each, in rank order."""
    width, height = _DIMS[profile.aspect]
    n = len(moments)

    steps.begin(4)
    plans: list[framing.CropPlan | None] = []
    for i, m in enumerate(moments):
        if cancel.is_set():
            raise JobCancelled()
        if not src.has_video:
            plans.append(None)  # audio only: the renderer draws an audiogram
        else:
            mode = m.framing if m.framing in FRAMINGS else opts.framing
            faces = None
            if providers.detect_faces is not None and mode == "cara":
                faces = providers.detect_faces(src, m.start, m.end, framing.SAMPLE_FPS, cancel)
            try:
                plans.append(framing.plan_crop(src, m.start, m.end, width, height, mode, cancel, faces=faces))
            except (JobCancelled, UserError):
                raise
            except Exception:  # scene or face analysis failed: a centred crop still makes a usable clip
                log.warning("Falló el encuadre de %s; se usa el centro", m.title, exc_info=True)
                plans.append(framing.plan_crop(src, m.start, m.end, width, height, "centro", cancel))
        steps.frac(4, (i + 1) / n)
    steps.end(4)

    steps.begin(5)
    ropts = RenderOptions(width, height, clip_fps(profile.fps, src.fps), ffmpeg.pick_encoder())
    rprofile = replace(profile, clipping=opts)
    for i, (m, plan) in enumerate(zip(moments, plans)):
        if cancel.is_set():
            raise JobCancelled()
        name = f"clip-{i + 1:02d}"
        workdir = folder / ".work" / name
        span = [w for w in words if w.end > m.start and w.start < m.end]
        try:
            render_clip(src, m, span, plan, rprofile, ropts, workdir, folder / f"{name}.mp4", cancel,
                        lambda f, i=i: steps.frac(5, (i + f) / n))
        except RuntimeError:  # FFmpeg failed: disk full, unreadable source... the details go to the log
            log.exception("Fallo al renderizar %s", name)
            raise UserError("No se pudo renderizar el video",
                            "Comprueba el espacio en disco y vuelve a intentarlo") from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    steps.end(5)


# What other unfinished queue items still need: (source ids, analysis ids). Supplied by the JobQueue.
Busy = Callable[[], "tuple[set[str], set[str]]"]


def _in_use(source_id: str, busy: Busy | None) -> bool:
    if busy is None:
        return False
    source_ids, analysis_ids = busy()
    if source_id in source_ids:
        return True
    for aid in analysis_ids:
        try:
            if json.loads((analyses_dir() / f"{aid}.json").read_text(encoding="utf-8")).get("source_id") == source_id:
                return True
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return False


def _release_source(src: SourceInfo, settings: dict, busy: Busy | None = None) -> None:
    """After a successful run the source (and the analyses that need it) go, unless the user keeps sources
    or another unfinished queue item still needs the source or one of its analyses."""
    if not settings.get("keep_sources") and not _in_use(src.id, busy):
        sources.delete_source(src.id)
        _delete_analyses_of(src.id)


def _source_of(analysis_file: Path) -> str | None:
    try:
        sid = json.loads(analysis_file.read_text(encoding="utf-8")).get("source_id")
    except (OSError, ValueError, AttributeError):
        return None
    return sid if isinstance(sid, str) else None


def source_needed(source_id: str, busy: Busy | None) -> bool:
    """An unfinished queue item or any saved analysis still needs this source."""
    if busy is not None and source_id in busy()[0]:
        return True
    return any(_source_of(f) == source_id for f in analyses_dir().glob("*.json"))


def release_source_id(source_id, settings: dict, busy: Busy | None) -> None:
    """A finished queue item no longer needs this source: it goes unless kept or still needed (ruling T8c)."""
    if sources.valid_id(source_id) and not settings.get("keep_sources") and not source_needed(source_id, busy):
        sources.delete_source(source_id)


def release_analysis(analysis_id, settings: dict, busy: Busy | None) -> None:
    """The finished analyze item that owns this analysis left the queue: the analysis goes unless an unfinished
    item still needs it, then its source under the same rule as release_source_id."""
    if not _valid_id(analysis_id) or (busy is not None and analysis_id in busy()[1]):
        return
    file = analyses_dir() / f"{analysis_id}.json"
    source_id = _source_of(file) if file.is_file() else None
    delete_analysis(analysis_id)
    if source_id is not None:
        release_source_id(source_id, settings, busy)


def sweep(analysis_ids: set[str], source_ids: set[str], keep_sources: bool) -> None:
    """Startup clean-up of what a crash or a removed queue item left behind: every analysis not in
    `analysis_ids` goes, then (unless sources are kept) every source folder that neither `source_ids` nor a
    remaining analysis needs, partial uploads and downloads included."""
    for f in analyses_dir().glob("*.json"):
        if _valid_id(f.stem) and f.stem not in analysis_ids:
            delete_analysis(f.stem)
    if keep_sources:
        return
    needed = set(source_ids) | {_source_of(f) for f in analyses_dir().glob("*.json")}
    for d in sources.sources_dir().iterdir():
        if sources.valid_id(d.name) and d.name not in needed:
            log.info("Borrando el video sin usar %s", d.name)
            sources.delete_source(d.name)


def _discard(folder: Path | None, fetched: SourceInfo | None) -> None:
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)
    if fetched is not None:
        sources.delete_source(fetched.id)


# ---------------------------------------------------------------- jobs

def run_clip_auto(item: dict, settings: dict, cancel: threading.Event, progress: ProgressFn,
                  providers: ClipProviders | None = None, *,
                  on_output: Callable[[Path], None] | None = None,
                  on_title: Callable[[str], None] | None = None,
                  busy: Busy | None = None) -> Path:
    """Link or stored source -> ranked clips in a new output folder (returned). A failed or cancelled run
    leaves no folder and no source this run downloaded."""
    providers = providers or ClipProviders()
    profile, opts = _resolve(item)
    steps = _Steps(progress, cancel)
    fetched: SourceInfo | None = None
    folder: Path | None = None
    try:
        steps.begin(1)
        src, is_new = _acquire_source(item, providers, cancel, lambda f: steps.frac(1, f))
        fetched = src if is_new else None
        if on_title is not None:
            on_title(src.title)
        steps.end(1)
        folder = _open_output(settings, src.title, on_output)
        transcript, moments = _understand(src, profile, opts, settings, cancel, steps, providers, opts.count)
        _render_all(src, moments, transcript.words, profile, opts, folder, cancel, steps, providers)
        _write_texts(folder, moments)
    except BaseException:
        _discard(folder, fetched)
        raise
    shutil.rmtree(folder / ".work", ignore_errors=True)
    _release_source(src, settings, busy)
    return folder


def run_clip_analyze(item: dict, settings: dict, cancel: threading.Event, progress: ProgressFn,
                     providers: ClipProviders | None = None, *,
                     on_output: Callable[[Path], None] | None = None,
                     on_title: Callable[[str], None] | None = None,
                  busy: Busy | None = None) -> Path:
    """Source -> transcript and up to 2 x count candidate moments saved as analisis/<id>.json (returned).
    The source stays for the export; it is removed only if this run fails after downloading it."""
    providers = providers or ClipProviders()
    profile, opts = _resolve(item)
    steps = _Steps(progress, cancel)
    fetched: SourceInfo | None = None
    try:
        steps.begin(1)
        src, is_new = _acquire_source(item, providers, cancel, lambda f: steps.frac(1, f))
        fetched = src if is_new else None
        if on_title is not None:
            on_title(src.title)
        steps.end(1)
        transcript, moments = _understand(src, profile, opts, settings, cancel, steps, providers, 2 * opts.count)
        path = analyses_dir() / f"{secrets.token_hex(6)}.json"
        config._write_json(path, {
            "source_id": src.id, "profile": opts.to_dict(), "transcript": transcript.to_dict(),
            "moments": [m.to_dict() for m in moments],
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
    except BaseException:
        _discard(None, fetched)
        raise
    return path


def run_clip_export(item: dict, settings: dict, cancel: threading.Event, progress: ProgressFn,
                    providers: ClipProviders | None = None, *,
                    on_output: Callable[[Path], None] | None = None,
                    on_title: Callable[[str], None] | None = None,
                  busy: Busy | None = None) -> Path:
    """The moments the user picked (with their edits) from a saved analysis -> clips in a new folder."""
    providers = providers or ClipProviders()
    profile, _ = _resolve(item, with_overrides=False)
    payload = _payload(item)
    steps = _Steps(progress, cancel)
    folder: Path | None = None
    try:
        steps.begin(1)
        analysis = load_analysis(payload.get("analysis_id"))
        try:
            src = sources.load_source(analysis["source_id"])
        except UserError:
            delete_analysis(payload.get("analysis_id"))  # useless without its source
            raise _original_gone() from None
        # the extras follow what the user sees at export time; the rest is what the analysis was made with
        opts = replace(ClipProfile.from_dict(analysis["profile"]), hook_title=profile.clipping.hook_title,
                       credit=profile.clipping.credit, music=profile.clipping.music)
        transcript = _transcript_from_dict(analysis["transcript"])
        raw = payload.get("moments")
        if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_EXPORT:
            raise UserError("Elige entre 1 y 20 clips para exportar", "Marca al menos un momento")
        try:
            moments = [_moment_from_dict(m) for m in raw]
        except ValueError:
            raise _bad_times() from None
        if any(not MIN_CLIP_S <= m.end - m.start <= MAX_CLIP_S for m in moments):
            raise _bad_times()  # browser-edited bounds: never a near-zero or hours-long clip
        if any(m.end > src.duration + 1.0 for m in moments):
            raise _bad_times()
        if on_title is not None:
            on_title(src.title)
        steps.end(1)
        folder = _open_output(settings, src.title, on_output)
        _render_all(src, moments, transcript.words, profile, opts, folder, cancel, steps, providers)
        _write_texts(folder, moments)
    except BaseException:
        _discard(folder, None)
        raise
    shutil.rmtree(folder / ".work", ignore_errors=True)
    _release_source(src, settings, busy)  # with keep_sources the analysis stays too: the user may export again
    return folder
