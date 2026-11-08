"""Word-level transcription: OpenAI whisper-1 (chunked) or local faster-whisper."""
from __future__ import annotations

import math
import tempfile
import threading
from pathlib import Path
from typing import Callable

import requests

from .. import config, ffmpeg
from ..ffmpeg import JobCancelled
from ..llm import _http
from ..models import UserError, Word
from .models import Transcript

CHUNK_S = 600.0  # seconds per OpenAI request (~1.8 MB each as 24 kbps opus)
MIN_WORDS = 20
MIN_TAIL_S = 0.5  # a final window shorter than this is merged into the previous one
OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"
_LANG_CODES = {"english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
               "portuguese": "pt", "dutch": "nl", "russian": "ru", "japanese": "ja", "korean": "ko",
               "chinese": "zh", "arabic": "ar", "hindi": "hi", "turkish": "tr", "polish": "pl",
               "catalan": "ca", "ukrainian": "uk", "swedish": "sv"}


def no_speech() -> UserError:
    return UserError("No se encontraron momentos con habla en este video",
                     "Prueba con otro video o elige el idioma a mano")


def models_dir() -> Path:
    d = config.data_dir() / "modelos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise JobCancelled()


def _finish(words: list[Word], language: str, duration: float) -> Transcript:
    clean = []
    for w in words:
        text = w.text.strip()
        if text:
            clean.append(Word(text, w.start, w.end))
    clean.sort(key=lambda w: w.start)
    if len(clean) < MIN_WORDS:
        raise no_speech()
    return Transcript(language=language, words=clean, duration=duration)


def transcribe(audio: Path, provider: str, language: str | None, settings: dict, model: str,
               cancel: threading.Event, on_progress: Callable[[float], None],
               on_model_load: Callable[[bool], None] | None = None) -> Transcript:
    if provider == "openai":
        return openai_words(audio, language, settings, cancel, on_progress)
    if provider == "local":
        return local_words(audio, language, model, cancel, on_progress, on_model_load=on_model_load)
    raise ValueError(f"unknown transcriber: {provider}")


def _windows(duration: float) -> list[tuple[float, float]]:
    """(start, length) windows of CHUNK_S; a tail under MIN_TAIL_S joins the previous window."""
    full = int(duration // CHUNK_S)
    tail = duration - full * CHUNK_S
    wins = [(i * CHUNK_S, CHUNK_S) for i in range(full)]
    if tail >= MIN_TAIL_S or not wins:
        wins.append((full * CHUNK_S, tail))
    else:
        wins[-1] = (wins[-1][0], CHUNK_S + tail)
    return wins


def _cut(audio: Path, start: float, length: float, out: Path, codec: list[str],
         cancel: threading.Event) -> None:
    ffmpeg.run(["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(audio), "-vn",
                "-ac", "1", "-ar", "16000", *codec, str(out)], cancel=cancel)


def openai_words(audio: Path, language: str | None, settings: dict, cancel: threading.Event,
                 on_progress: Callable[[float], None]) -> Transcript:
    key = settings.get("openai_key", "")
    if not key:
        raise UserError("Falta la clave de OpenAI", "Ajústala en Ajustes o usa la transcripción local")
    duration = float(ffmpeg.probe(audio)["duration"])
    wins = _windows(duration)
    data = [("model", "whisper-1"), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word")]
    if language:
        data.append(("language", language))
    words: list[Word] = []
    detected = language
    with tempfile.TemporaryDirectory(prefix="baitd-stt-") as tmp:
        for i, (start, length) in enumerate(wins):
            _check(cancel)
            chunk = Path(tmp) / f"chunk{i}.ogg"
            _cut(audio, start, length, chunk, ["-c:a", "libopus", "-b:a", "24k"], cancel)
            _check(cancel)
            with open(chunk, "rb") as fh:
                resp = _http.request("post", OPENAI_URL, "OpenAI", "whisper-1",
                                     headers={"Authorization": f"Bearer {key}"}, data=data,
                                     files={"file": (chunk.name, fh, "audio/ogg")})
            body = _http.decode_json(resp, "OpenAI")
            if not isinstance(body, dict):
                raise _http.unexpected("OpenAI")
            if detected is None:
                name = str(body.get("language") or "").lower()
                detected = _LANG_CODES.get(name, name or "en")
            for w in body.get("words") or []:
                words.append(Word(str(w["word"]), float(w["start"]) + start, float(w["end"]) + start))
            on_progress(min(1.0, (i + 1) / len(wins)))
    return _finish(words, detected or "en", duration)


def _load_model(model: str):
    from faster_whisper import WhisperModel  # lazy: heavy import, optional at test time
    return WhisperModel(model, device="cpu", compute_type="int8", download_root=str(models_dir()))


def local_words(audio: Path, language: str | None, model: str, cancel: threading.Event,
                on_progress: Callable[[float], None],
                on_model_load: Callable[[bool], None] | None = None) -> Transcript:
    """`on_model_load(True)` before the model loads (downloaded into modelos/ on first use, which can take
    minutes and cannot be interrupted), `on_model_load(False)` once it is ready."""
    _check(cancel)
    if on_model_load is not None:
        on_model_load(True)
    try:
        wm = _load_model(model)
    except Exception as e:
        raise UserError("No se pudo descargar el modelo de transcripción",
                        "Revisa tu conexión e inténtalo de nuevo") from e
    _check(cancel)
    if on_model_load is not None:
        on_model_load(False)
    duration = float(ffmpeg.probe(audio)["duration"])
    words: list[Word] = []
    lang = language
    first_lang = None
    with tempfile.TemporaryDirectory(prefix="baitd-stt-") as tmp:
        for i, (start, length) in enumerate(_windows(duration)):
            _check(cancel)
            chunk = Path(tmp) / f"chunk{i}.wav"
            _cut(audio, start, length, chunk, ["-c:a", "pcm_s16le"], cancel)
            _check(cancel)
            segments, info = wm.transcribe(str(chunk), language=lang, word_timestamps=True, vad_filter=True)
            if first_lang is None:
                first_lang = language or getattr(info, "language", None) or "en"
                lang = lang or first_lang
            for seg in segments:
                _check(cancel)
                for w in seg.words or []:
                    words.append(Word(w.word, float(w.start) + start, float(w.end) + start))
                on_progress(min(1.0, (start + seg.end) / duration))
            chunk.unlink(missing_ok=True)
    return _finish(words, first_lang or "en", duration)
