"""Clipping jobs for the queue: auto (link -> clips), analyze (link -> candidate moments on disk), export
(chosen moments -> clips) and live_title (the AI's title for a saved live moment). Every network- or
model-facing step can be replaced through ClipProviders, so the self-test and the tests run offline."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import config, ffmpeg, llm
from ..models import ASPECTS, JobCancelled, Profile, ProgressFn, RenderOptions, UserError, Word
from ..pipeline import _create_output_dir
from . import camaras, chat_replay, framing, sources, transcribe, voices
from .models import BADGES, FRAMINGS, ClipProfile, Moment, SourceInfo, Transcript
from .moments import TITLE_WORDS, _sentences, find_moments, snap_to_sentences
from .render import clip_fps, render_clip

__all__ = ["CLIP_STEPS", "CLIP_STEP_LABELS", "MODEL_STEP", "CLIP_JOB_KINDS", "ClipProviders", "analyses_dir",
           "run_clip_auto", "run_clip_analyze", "run_clip_export", "run_live_title", "load_analysis",
           "delete_analysis"]

log = logging.getLogger(__name__)

CLIP_STEPS = ("source", "transcribe", "moments", "framing", "render")
MODEL_STEP = "model"  # step 2's text while the local whisper model loads (downloaded on first use)
CLIP_STEP_LABELS = (*CLIP_STEPS, MODEL_STEP)  # every step name a clip job can show (step.<name> in i18n)
CLIP_JOB_KINDS = ("clip_auto", "clip_analyze", "clip_export", "live_title")
_TOTAL = len(CLIP_STEPS)
_ID = re.compile(r"[0-9a-f]{12}")
MIN_CLIP_S, MAX_CLIP_S = 10.0, 180.0  # clip length limits
_MAX_EXPORT = 20  # same cap as the clip count of a run


@dataclass
class ClipProviders:
    """Replaceable network- or model-facing steps; None means the real implementation."""
    fetch_url: Callable | None = None     # (url, cancel, on_progress) -> SourceInfo
    transcribe: Callable | None = None    # (audio, provider, language, settings, model, cancel, on_progress,
                                          #  on_model_load=None) -> Transcript
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
    badge = d.get("badge", "")
    if not isinstance(badge, str) or badge not in ("", *BADGES):
        raise ValueError("bad badge")
    cam = d.get("cam")
    if cam is not None and not (isinstance(cam, list) and len(cam) == 4
                                and all(type(v) is int and v >= 0 for v in cam)):
        raise ValueError("cam must be null or four non-negative integers")
    return Moment(start=start, end=end, kind=_text(d, "kind", "interesante"),
                  score=round(_num(d.get("score", 0))), title=_text(d, "title"),
                  description=_text(d, "description"), hashtags=list(tags), text=_text(d, "text"),
                  framing=framing_, credit=_text(d, "credit"), badge=badge,
                  cam=None if cam is None else list(cam))


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

    def show(self, i: int, label: str) -> None:
        """Step i under another text (e.g. MODEL_STEP); the next frac/end shows the step's own text again."""
        self._progress(label, i, _TOTAL, 0.0)


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
                    on_progress: Callable[[float], None],
                    on_source: Callable[[dict], None] | None = None) -> tuple[SourceInfo, bool]:
    """(source, delete it if the job fails). A source the user already has is never deleted on failure, nor
    is a download the queue item now points at (on_source): Reintentar reuses it instead of downloading
    again. A retry whose download is gone fetches the link again."""
    s = _payload(item).get("source")
    s = s if isinstance(s, dict) else {}
    url = s["url"].strip() if isinstance(s.get("url"), str) else ""
    if s.get("fetched") is True and isinstance(s.get("source_id"), str):
        try:
            return sources.load_source(s["source_id"]), False
        except UserError:
            if not url:
                raise
            log.info("El video descargado %s ya no está; se descarga otra vez", s["source_id"])
            sources.delete_source(s["source_id"])  # whatever is left of it is unusable
    if url:
        sources.check_url(url)
        src = (providers.fetch_url or sources.fetch_url)(url, cancel, on_progress)
        if on_source is None:
            return src, True
        on_source({"source_id": src.id, "fetched": True, "url": url})
        return src, False
    if isinstance(s.get("source_id"), str):
        return sources.load_source(s["source_id"]), False
    raise UserError("Falta el video de origen", "Pega un enlace o sube un archivo")


_TRANSCRIPT = "transcript.json"  # in the source folder: a retry or a new analysis skips transcribing


def _transcript_key(opts: ClipProfile, language: str | None) -> list:
    model = "whisper-1" if opts.transcriber == "openai" else opts.whisper_model
    return [opts.transcriber, model, language]


def _saved_transcript(src: SourceInfo, key: list) -> Transcript | None:
    """The transcript saved with the source when it was made with the same settings; None otherwise."""
    try:
        raw = json.loads((sources.sources_dir() / src.id / _TRANSCRIPT).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("key") == key:
            return _transcript_from_dict(raw.get("transcript"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        pass
    return None


def _save_transcript(src: SourceInfo, key: list, transcript: Transcript) -> None:
    if not sources.valid_id(src.id) or not (sources.sources_dir() / src.id).is_dir():
        return
    try:
        config._write_json(sources.sources_dir() / src.id / _TRANSCRIPT,
                           {"key": key, "transcript": transcript.to_dict()})
    except OSError:
        log.warning("No se pudo guardar la transcripción de %s", src.id)


LIVE_CPU_THREADS = 2  # the local transcriber's threads for a live moment: a stream may be running on this PC


def _live_scope() -> ExitStack:
    """What a live moment's export or AI title runs inside (spec §4): FFmpeg and this job thread (face
    detection runs on it too) below normal priority. The thread's priority comes back afterwards."""
    from ..live import signals  # the live package imports this module
    stack = ExitStack()
    stack.enter_context(ffmpeg.low_priority())
    stack.enter_context(signals.below_normal_scope())
    return stack


def _live_opts(stored: ClipProfile, profile: Profile) -> ClipProfile:
    """A live moment's analysis keeps a default ClipProfile (nothing was transcribed when it was saved): the
    transcriber, Whisper model and language are the ones the profile has now, at export or title time."""
    c = profile.clipping
    return replace(stored, transcriber=c.transcriber, whisper_model=c.whisper_model, language=c.language)


def _transcribe(src: SourceInfo, opts: ClipProfile, settings: dict, cancel: threading.Event, steps: _Steps,
                providers: ClipProviders, *, audio: Path | None = None, cpu_threads: int | None = None) -> Transcript:
    """Step 2: the source's transcript, read back from the source folder when it was made with the same
    settings. `audio` is where to extract the sound for a caller that needs it afterwards (it is then always
    extracted); without it the sound goes to a temp file, and only when there is no saved transcript."""
    steps.begin(2)
    if audio is not None:
        sources.extract_audio(src, audio, cancel)
    language = None if opts.language == "auto" else opts.language
    key = _transcript_key(opts, language)
    transcript = _saved_transcript(src, key)
    if transcript is None:
        with tempfile.TemporaryDirectory(prefix="baitd-clip-") as tmp:
            wav = audio if audio is not None else sources.extract_audio(src, Path(tmp) / "audio.wav", cancel)
            extra = {"cpu_threads": cpu_threads} if cpu_threads else {}
            transcript = (providers.transcribe or transcribe.transcribe)(
                wav, opts.transcriber, language, settings, opts.whisper_model, cancel,
                lambda f: steps.frac(2, f),
                on_model_load=lambda loading: steps.show(2, MODEL_STEP) if loading else steps.frac(2, 0.0), **extra)
        _save_transcript(src, key, transcript)
    steps.end(2)
    return transcript


def _chat_of(item: dict, src: SourceInfo) -> list[tuple[float, str]] | None:
    """The stream's chat: the file the user gave (Twitch), else the replay yt-dlp saved with the video
    (YouTube). None without one; a saved replay that cannot be read only means no chat."""
    chat_id = _payload(item).get("chat_id")
    if chat_id is not None:
        return chat_replay.load(chat_id)
    replay = sources.chat_replay_path(src)
    if replay is None:
        return None
    try:
        return chat_replay.parse(replay.read_text("utf-8"))
    except (UserError, OSError) as e:
        log.info("El chat del directo no sirve (%s): se buscan los momentos con la IA", e)
        return None


CHAT_BEFORE_S, CHAT_AFTER_S = 40.0, 30.0  # around a chat peak's 10 s bin: what made the chat react comes first
_JOIN_SILENCE_S = 1.0  # between windows in the joined audio: longer than a sentence pause, so no sentence spans two


def _windows_audio(src: SourceInfo, windows: list[tuple[float, float]], out: Path, cancel: threading.Event
                   ) -> list[float]:
    """The windows' sound one after another (with silence between) in one WAV, so the model loads once.
    Returns where each window starts in it."""
    import wave
    starts, frames = [], []
    for i, (w0, w1) in enumerate(windows):
        part = out.with_name(f"w{i}.wav")
        try:
            ffmpeg.run(["-ss", f"{w0:.3f}", "-t", f"{w1 - w0:.3f}", "-i", str(src.path), "-map", "0:a:0", "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(part)], cancel=cancel)
        except RuntimeError:
            raise UserError("No se pudo leer el audio del video", "Comprueba que el archivo tiene sonido") from None
        with wave.open(str(part)) as w:
            starts.append(sum(len(f) for f in frames) / 2 / 16000)
            frames.append(w.readframes(w.getnframes()) + b"\0\0" * int(_JOIN_SILENCE_S * 16000))
        part.unlink()
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(frames))
    return starts


def _understand_chat(src: SourceInfo, profile: Profile, opts: ClipProfile, settings: dict, cancel: threading.Event,
                     steps: _Steps, providers: ClipProviders, want: int, chat: list[tuple[float, str]]
                     ) -> tuple[Transcript, list[Moment]]:
    """Steps 2 and 3 from the chat: only the windows around its peaks are transcribed, and the AI reads each
    window once for its kind, span and title. Without an answer a peak is still a moment, named by its words."""
    from ..live import ai  # ai imports this module
    peaks = [p for p in chat_replay.peaks(chat, want) if p.t < src.duration]
    if not peaks:
        raise UserError("El chat de este directo no tiene picos claros",
                        "Quita el chat y Baitd buscará los momentos con la IA")
    peaks.sort(key=lambda p: p.t)
    windows = [(max(0.0, p.t - CHAT_BEFORE_S), min(src.duration, p.t + CHAT_AFTER_S)) for p in peaks]
    language = None if opts.language == "auto" else opts.language
    steps.begin(2)
    with tempfile.TemporaryDirectory(prefix="baitd-clip-") as tmp:
        wav = Path(tmp) / "audio.wav"
        starts = _windows_audio(src, windows, wav, cancel)
        heard = (providers.transcribe or transcribe.transcribe)(
            wav, opts.transcriber, language, settings, opts.whisper_model, cancel, lambda f: steps.frac(2, f),
            on_model_load=lambda loading: steps.show(2, MODEL_STEP) if loading else steps.frac(2, 0.0))
    words = []
    for w in heard.words:  # back to the video's clock: the window each word starts in
        k = max(i for i, s in enumerate(starts) if s <= w.start + 1e-6) if w.start >= starts[0] else 0
        shift = windows[k][0] - starts[k]
        if w.start + shift < windows[k][1]:  # one placed in the silence after a window belongs to no window
            words.append(Word(w.text, w.start + shift, min(w.end + shift, windows[k][1])))
    steps.end(2)

    steps.begin(3)
    lang = heard.language or language or "the transcript's own"
    complete = providers.complete or (lambda p: llm.complete(p, profile, settings, cancel=cancel))
    top = max(p.messages for p in peaks)
    moments = []
    for i, (p, (w0, w1)) in enumerate(zip(peaks, windows)):
        lines = ai.window_lines(words, w0, w1)
        verdict = None
        if lines:
            try:
                verdict = ai.parse_reply(complete(ai.build_prompt(lines, lang, want_span=True)), w0, w1,
                                         want_span=True)
            except JobCancelled:
                raise
            except UserError as e:  # no key, quota, blocked: the peak stays, unnamed by the AI
                log.warning("La IA no respondió por un pico del chat: %s", e.cause)
        s, e = (verdict.start, verdict.end) if verdict is not None else (w0, w1)
        s, e = snap_to_sentences(s, e, words, opts.min_s, opts.max_s) or (w0, min(w1, w0 + opts.max_s))
        text = " ".join(w.text for w in words if s - 1e-6 <= w.start < e)
        kind = verdict.kind if verdict is not None and verdict.kind != "nada" else (
            "gracioso" if p.reason == "laughs" else "interesante")
        moments.append(Moment(
            start=s, end=e, kind=kind, score=round(100 * p.messages / top),
            title=(verdict.title if verdict is not None else "") or " ".join(text.split()[:TITLE_WORDS]),
            description=verdict.description if verdict is not None else "",
            hashtags=verdict.hashtags if verdict is not None else [], text=text, framing=opts.framing))
        steps.frac(3, (i + 1) / len(peaks))
    steps.end(3)
    return (Transcript(heard.language or (language or ""), words, src.duration),
            sorted(moments, key=lambda m: -m.score))


def _want(opts: ClipProfile) -> int:
    """How many clips a run makes: the count, or for "Todos" (0) every good moment found up to _MAX_EXPORT."""
    return opts.count or _MAX_EXPORT


def _understand(src: SourceInfo, profile: Profile, opts: ClipProfile, settings: dict, cancel: threading.Event,
                steps: _Steps, providers: ClipProviders, want: int,
                chat: list[tuple[float, str]] | None = None) -> tuple[Transcript, list[Moment]]:
    """Steps 2 and 3: audio + transcript, then the ranked moments. Big files live in a temp folder. With the
    stream's chat, its peaks pick the moments instead (_understand_chat)."""
    if chat:
        return _understand_chat(src, profile, opts, settings, cancel, steps, providers, want, chat)
    with tempfile.TemporaryDirectory(prefix="baitd-clip-") as tmp:
        audio = Path(tmp) / "audio.wav"  # the moments step needs it too
        transcript = _transcribe(src, opts, settings, cancel, steps, providers, audio=audio)

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


def _publish_text(clips: list[dict]) -> str:
    """publicacion.txt from the clips.json entries: per clip its file, span, title, description and hashtags.
    The entries may come back from disk, so a field of the wrong type is left out."""
    def text(c: dict, key: str) -> str:
        return c[key] if isinstance(c.get(key), str) else ""

    def sec(v) -> float:
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0

    blocks = []
    for c in clips:
        if not isinstance(c, dict):
            continue
        tags = c.get("hashtags") if isinstance(c.get("hashtags"), list) else []
        tags = " ".join(t if t.startswith("#") else f"#{t}" for t in tags if isinstance(t, str) and t)
        blocks.append(f"{text(c, 'file')} ({_hms(sec(c.get('start')))}–{_hms(sec(c.get('end')))})\n"
                      f"{text(c, 'title')}\n\n{text(c, 'description')}\n\n{tags}\n")
    return "\n".join(blocks)


def _write_texts(folder: Path, moments: list[Moment]) -> None:
    clips = [{"file": f"clip-{i:02d}.mp4", "title": m.title, "description": m.description,
              "hashtags": list(m.hashtags), "start": m.start, "end": m.end, "text": m.text}
             for i, m in enumerate(moments, 1)]
    (folder / "publicacion.txt").write_text(_publish_text(clips), encoding="utf-8")
    (folder / "clips.json").write_text(json.dumps(clips, ensure_ascii=False, indent=2), encoding="utf-8")


RETITLE_TEXT_CHARS = 4000  # enough of what is said in a clip; keeps the request small


def _retitle_prompt(clip: dict, language: str) -> str:
    def field(key: str) -> str:
        return clip[key].strip() if isinstance(clip.get(key), str) else ""

    said = field("text")[:RETITLE_TEXT_CHARS]
    return "\n".join([
        "You write the texts to post a vertical short clip (TikTok, Reels, Shorts) cut from a longer video.",
        f"Its current title is: {field('title') or '(none)'}",
        f"Its current description is: {field('description') or '(none)'}",
        *([f"What is said in the clip: {said}"] if said else []),
        f"Write a NEW title, different from the current one (at most {TITLE_WORDS} words), a description "
        f"(one sentence) and 3 to 6 hashtags, in this language: {language}.",
        'Reply with JSON only, no commentary: {"title":"...","description":"...","hashtags":["..."]}',
    ])


def _retitle_reply(raw, old_title: str) -> dict | None:
    """The reply as {title, description, hashtags}; None when unreadable or the title did not change."""
    from ..live.ai import _object  # live.ai imports this module
    data = _object(raw) if isinstance(raw, str) else None
    if data is None:
        return None
    title, desc, tags = data.get("title"), data.get("description", ""), data.get("hashtags", [])
    if not isinstance(title, str) or not isinstance(desc, str) or not isinstance(tags, list):
        return None
    title = " ".join(title.split()[:TITLE_WORDS])
    if not title or title.casefold() == " ".join(old_title.split()).casefold():
        return None
    tags = [t.strip().lstrip("#") for t in tags if isinstance(t, str)]
    return {"title": title, "description": desc.strip(),
            "hashtags": ["#" + re.sub(r"\s+", "", t) for t in tags if t]}


def _replace_file(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def retitle_clip(item: dict, folder: Path, file: str, settings: dict,
                 complete: Callable[[str], str] | None = None) -> dict | None:
    """A new title, description and hashtags for one finished clip, saved to clips.json and publicacion.txt.
    None when the clip is not in clips.json. One retry when the reply is unreadable or keeps the old title."""
    path = folder / "clips.json"
    try:
        clips = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    entry = next((c for c in clips if isinstance(c, dict) and c.get("file") == file), None) \
        if isinstance(clips, list) else None
    if entry is None:
        return None
    profile, opts = _resolve(item)
    complete = complete or (lambda p: llm.complete(p, profile, settings))
    old = entry["title"] if isinstance(entry.get("title"), str) else ""
    language = opts.language if opts.language != "auto" else "the same language as the current title"
    prompt = _retitle_prompt(entry, language)
    new = _retitle_reply(complete(prompt), old)
    if new is None:
        new = _retitle_reply(complete(f"{prompt}\n\nReturn only valid JSON matching the schema, "
                                      "with a title different from the current one."), old)
    if new is None:
        raise _no_title()
    entry.update(new)
    _replace_file(path, json.dumps(clips, ensure_ascii=False, indent=2))
    _replace_file(folder / "publicacion.txt", _publish_text(clips))
    return new


VOICE_CONTEXT_S = 90.0  # sentences this far around a clip help tell its two voices apart


def _talk_and_voices(src: SourceInfo, m: Moment, words: list[Word], mode: str, cancel: threading.Event
                     ) -> tuple[list[tuple[float, float]] | None, list[int | None] | None]:
    """The clip's sentences (clip-relative spans) and, for the face crop, which of two voices says each
    one (None when the voices do not split clearly). With two people in a shot the crop follows the
    speaker: by voice when possible, else by mouth motion."""
    near = [w for w in words if w.end > m.start - VOICE_CONTEXT_S and w.start < m.end + VOICE_CONTEXT_S]
    sentences = [(s0, s1) for s0, s1, _, _ in _sentences(near)]
    inside = [i for i, (s0, s1) in enumerate(sentences) if s1 > m.start and s0 < m.end]
    talk = [(sentences[i][0] - m.start, sentences[i][1] - m.start) for i in inside] or None
    if not talk or mode != "cara":
        return talk, None
    try:
        labels, _ = voices.sentence_voices(src.path, sentences, cancel)
    except JobCancelled:
        raise
    except Exception:  # the mouth-motion fallback still makes a usable crop
        log.warning("No se pudieron separar las voces de %s", m.title, exc_info=True)
        return talk, None
    return talk, [labels[i] for i in inside]


def cam_channel(src: SourceInfo, analysis: dict | None = None) -> str:
    """The channel a remembered cam box belongs to: a live moment's handle, else the source's channel."""
    if analysis and analysis.get("origin") == "live":  # a live source's own channel field is ""
        return str((analysis.get("live") or {}).get("handle") or "")
    return src.channel


def _render_all(src: SourceInfo, moments: list[Moment], words: list[Word], profile: Profile, opts: ClipProfile,
                folder: Path, cancel: threading.Event, steps: _Steps, providers: ClipProviders,
                channel: str | None = None) -> None:
    """Steps 4 and 5: a crop plan for every moment, then one rendered clip each, in rank order. A cam box
    remembered for `channel` (default: the source's) frames its "cara" and "partido" moments as "partido"."""
    width, height = ASPECTS[profile.aspect]
    n = len(moments)
    remembered = camaras.load(src.channel if channel is None else channel) if src.has_video else None

    steps.begin(4)
    plans: list[framing.CropPlan | None] = []
    for i, m in enumerate(moments):
        if cancel.is_set():
            raise JobCancelled()
        if not src.has_video:
            plans.append(None)  # audio only: the renderer draws an audiogram
        else:
            mode = m.framing if m.framing in FRAMINGS else opts.framing
            cam = m.cam
            if cam is None and remembered is not None and mode in ("cara", "partido"):
                mode, cam = "partido", camaras.scaled(remembered, src.width, src.height)
            faces = None
            if providers.detect_faces is not None and (mode == "cara" or (mode == "partido" and cam is None)):
                faces = providers.detect_faces(src, m.start, m.end, framing.SAMPLE_FPS, cancel)
            talk, voice_labels = (None, None) if mode == "partido" else _talk_and_voices(src, m, words, mode, cancel)
            try:
                plans.append(framing.plan_crop(src, m.start, m.end, width, height, mode, cancel, faces=faces,
                                               talk=talk, voices=voice_labels, cam=cam))
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
    item still needs it, then its source under the same rule as release_source_id. A live moment never goes
    this way: removing a queue item leaves it (and its video) in the live list."""
    if not _valid_id(analysis_id) or (busy is not None and analysis_id in busy()[1]):
        return
    file = analyses_dir() / f"{analysis_id}.json"
    if _is_live(file):  # a live moment belongs to the live list, not to the queue item that used it
        return
    source_id = _source_of(file) if file.is_file() else None
    delete_analysis(analysis_id)
    if source_id is not None:
        release_source_id(source_id, settings, busy)


def _is_live(analysis_file: Path) -> bool:
    """A moment cut from a live capture: no queue item lists it, yet the user still has to review it."""
    try:
        return json.loads(analysis_file.read_text(encoding="utf-8")).get("origin") == "live"
    except (OSError, ValueError, AttributeError):
        return False


def sweep(analysis_ids: set[str], source_ids: set[str], keep_sources: bool) -> None:
    """Startup clean-up of what a crash or a removed queue item left behind: every analysis not in
    `analysis_ids` goes, then (unless sources are kept) every source folder that neither `source_ids` nor a
    remaining analysis needs. Partial uploads and downloads (no meta.json: never finished before this start)
    go even when sources are kept, since they are not sources yet."""
    for f in analyses_dir().glob("*.json"):
        if _valid_id(f.stem) and f.stem not in analysis_ids and not _is_live(f):
            delete_analysis(f.stem)
    needed = set(source_ids) | {_source_of(f) for f in analyses_dir().glob("*.json")}
    for d in sources.sources_dir().iterdir():
        if not sources.valid_id(d.name) or d.name in needed:
            continue
        if not (d / "meta.json").is_file():
            log.info("Borrando la subida o descarga sin terminar %s", d.name)
            sources.delete_source(d.name)
        elif not keep_sources:
            log.info("Borrando el video sin usar %s", d.name)
            sources.delete_source(d.name)


def _discard(folder: Path | None, fetched: SourceInfo | None) -> None:
    """A failed run's output folder goes, and the source it downloaded unless the queue item points at it
    (then `fetched` is None: _acquire_source decides)."""
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)
    if fetched is not None:
        sources.delete_source(fetched.id)


# ---------------------------------------------------------------- jobs

def run_clip_auto(item: dict, settings: dict, cancel: threading.Event, progress: ProgressFn,
                  providers: ClipProviders | None = None, *,
                  on_output: Callable[[Path], None] | None = None,
                  on_title: Callable[[str], None] | None = None,
                  on_source: Callable[[dict], None] | None = None,
                  busy: Busy | None = None) -> Path:
    """Link or stored source -> ranked clips in a new output folder (returned). A failed or cancelled run
    leaves no folder; a source it downloaded stays only if on_source recorded it for Reintentar."""
    providers = providers or ClipProviders()
    profile, opts = _resolve(item)
    steps = _Steps(progress, cancel)
    fetched: SourceInfo | None = None
    folder: Path | None = None
    try:
        steps.begin(1)
        src, disposable = _acquire_source(item, providers, cancel, lambda f: steps.frac(1, f), on_source)
        fetched = src if disposable else None
        if on_title is not None:
            on_title(src.title)
        steps.end(1)
        folder = _open_output(settings, src.title, on_output)
        transcript, moments = _understand(src, profile, opts, settings, cancel, steps, providers, _want(opts),
                                         _chat_of(item, src))
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
                     on_source: Callable[[dict], None] | None = None,
                     busy: Busy | None = None) -> Path:
    """Source -> transcript and up to 2 x count candidate moments saved as analisis/<id>.json (returned).
    The source stays for the export; if this run fails after downloading it, it stays only if on_source
    recorded it for Reintentar."""
    providers = providers or ClipProviders()
    profile, opts = _resolve(item)
    steps = _Steps(progress, cancel)
    fetched: SourceInfo | None = None
    try:
        steps.begin(1)
        src, disposable = _acquire_source(item, providers, cancel, lambda f: steps.frac(1, f), on_source)
        fetched = src if disposable else None
        if on_title is not None:
            on_title(src.title)
        steps.end(1)
        transcript, moments = _understand(src, profile, opts, settings, cancel, steps, providers, 2 * _want(opts),
                                         _chat_of(item, src))
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
                    on_source: Callable[[dict], None] | None = None,
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
        live = analysis.get("origin") == "live"
        opts = replace(ClipProfile.from_dict(analysis["profile"]), hook_title=profile.clipping.hook_title,
                       credit=profile.clipping.credit, music=profile.clipping.music)
        if live:  # a live moment was never transcribed: it is, with the settings the user has now
            opts = _live_opts(opts, profile)
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
        with _live_scope() if live else nullcontext():  # a capture may be running on this PC
            if live and not transcript.words:  # not transcribed while capturing
                transcript = _transcribe(src, opts, settings, cancel, steps, providers,
                                         cpu_threads=LIVE_CPU_THREADS)
                _keep_transcript(payload.get("analysis_id"), transcript)
            if live:  # the user's marks, moved only to the nearest whole sentences
                if all(_stand_in_title(analysis, m.title) for m in moments):  # "Samulx · 00:10" is no hook
                    opts = replace(opts, hook_title=False)
                for m in moments:
                    snapped = snap_to_sentences(m.start, m.end, transcript.words, MIN_CLIP_S, MAX_CLIP_S)
                    if snapped is not None:
                        m.start, m.end = snapped
            folder = _open_output(settings, src.title, on_output)
            _render_all(src, moments, transcript.words, profile, opts, folder, cancel, steps, providers,
                        cam_channel(src, analysis))
            _write_texts(folder, moments)
    except BaseException:
        _discard(folder, None)
        raise
    shutil.rmtree(folder / ".work", ignore_errors=True)
    if live:  # an exported live moment leaves the live list; its video follows keep_sources as any source
        delete_analysis(payload.get("analysis_id"))
    _release_source(src, settings, busy)  # with keep_sources an ordinary analysis stays: the user may export again
    return folder


def _stand_in_title(analysis: dict, title: str) -> bool:
    """The name a live moment gets when saved ("<channel> · HH:MM"), still unchanged: no AI title yet and
    not retyped by the user."""
    live = analysis.get("live") or {}
    at = str(live.get("at") or "")  # ISO, local time: the HH:MM the name was made from
    return not live.get("classified") and len(at) >= 16 and title.endswith(f" · {at[11:16]}")


def _keep_transcript(analysis_id: str, transcript: Transcript) -> None:
    """A live moment's transcript goes into its analysis: the next export or AI title reads it back."""
    from ..live import store  # store imports this module
    store.update_moment(analysis_id, transcript=transcript.to_dict())


def _no_title() -> UserError:
    return UserError("La IA no devolvió un título válido", "Inténtalo de nuevo o cambia de modelo en Ajustes")


def _silent_moment() -> UserError:
    return UserError("En este momento no se oye a nadie hablar", "Escribe el título a mano")


def run_live_title(item: dict, settings: dict, cancel: threading.Event, progress: ProgressFn,
                   providers: ClipProviders | None = None, *,
                   on_output: Callable[[Path], None] | None = None,
                   on_title: Callable[[str], None] | None = None,
                   on_source: Callable[[dict], None] | None = None,
                   busy: Busy | None = None) -> Path:
    """The AI's kind, title, description and hashtags for one saved live moment (asked for in review). The
    moment's start and end never change. Returns the analysis file; the source and the analysis stay."""
    from ..live import ai, store  # store imports this module
    providers = providers or ClipProviders()
    profile, _ = _resolve(item, with_overrides=False)
    analysis_id = _payload(item).get("analysis_id")
    steps = _Steps(progress, cancel)

    steps.begin(1)
    analysis = load_analysis(analysis_id)
    if analysis.get("origin") != "live":
        raise _original_gone()
    src = sources.load_source(analysis["source_id"])
    if on_title is not None:
        on_title(src.title)
    opts = _live_opts(ClipProfile.from_dict(analysis["profile"]), profile)
    transcript = _transcript_from_dict(analysis["transcript"])
    steps.end(1)

    if not transcript.words:
        with _live_scope():  # a capture may be running on this PC
            transcript = _transcribe(src, opts, settings, cancel, steps, providers, cpu_threads=LIVE_CPU_THREADS)
        _keep_transcript(analysis_id, transcript)

    steps.begin(3)
    m = analysis["moments"][0]
    lines = ai.window_lines(transcript.words, m["start"], m["end"])
    if not lines:
        raise _silent_moment()
    language = transcript.language or (opts.language if opts.language != "auto" else "the transcript's own")
    complete = providers.complete or (lambda p: llm.complete(p, profile, settings, cancel=cancel))
    verdict = ai.parse_reply(complete(ai.build_prompt(lines, language, want_span=False)), m["start"], m["end"],
                             want_span=False)
    if verdict is None or not verdict.title:
        raise _no_title()
    steps.end(3)

    fields: dict = {"title": verdict.title, "classified": True}
    if verdict.description:
        fields["description"] = verdict.description
    if verdict.hashtags:
        fields["hashtags"] = verdict.hashtags
    if verdict.kind != "nada":
        fields["kind"] = verdict.kind
    store.update_moment(analysis_id, **fields)
    return analyses_dir() / f"{analysis_id}.json"
