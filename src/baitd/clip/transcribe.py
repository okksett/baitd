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
               cancel: threading.Event, on_progress: Callable[[float], None]) -> Transcript:
    if provider == "openai":
        return openai_words(audio, language, settings, cancel, on_progress)
    if provider == "local":
        return local_words(audio, language, model, cancel, on_progress)
    raise ValueError(f"unknown transcriber: {provider}")


def openai_words(audio: Path, language: str | None, settings: dict, cancel: threading.Event,
                 on_progress: Callable[[float], None]) -> Transcript:
    key = settings.get("openai_key", "")
    if not key:
        raise UserError("Falta la clave de OpenAI", "Ajústala en Ajustes o usa la transcripción local")
    duration = float(ffmpeg.probe(audio)["duration"])
    n = max(1, math.ceil(duration / CHUNK_S))
    data = [("model", "whisper-1"), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word")]
    if language:
        data.append(("language", language))
    words: list[Word] = []
    detected = language
    with tempfile.TemporaryDirectory(prefix="baitd-stt-") as tmp:
        for i in range(n):
            _check(cancel)
            start = i * CHUNK_S
            chunk = Path(tmp) / f"chunk{i}.ogg"
            ffmpeg.run(["-ss", f"{start:.3f}", "-t", f"{CHUNK_S:.3f}", "-i", str(audio), "-vn",
                        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(chunk)], cancel=cancel)
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
            on_progress(min(1.0, (i + 1) / n))
    return _finish(words, detected or "en", duration)


def _load_model(model: str):
    from faster_whisper import WhisperModel  # lazy: heavy import, optional at test time
    return WhisperModel(model, device="cpu", compute_type="int8", download_root=str(models_dir()))


def local_words(audio: Path, language: str | None, model: str, cancel: threading.Event,
                on_progress: Callable[[float], None]) -> Transcript:
    try:
        wm = _load_model(model)
    except Exception as e:
        raise UserError("No se pudo descargar el modelo de transcripción",
                        "Revisa tu conexión e inténtalo de nuevo") from e
    _check(cancel)
    segments, info = wm.transcribe(str(audio), language=language, word_timestamps=True, vad_filter=True)
    duration = float(getattr(info, "duration", 0) or 0)
    if duration <= 0:
        duration = float(ffmpeg.probe(audio)["duration"])
    words: list[Word] = []
    for seg in segments:
        _check(cancel)
        for w in seg.words or []:
            words.append(Word(w.word, float(w.start), float(w.end)))
        on_progress(min(1.0, seg.end / duration))
    return _finish(words, language or getattr(info, "language", None) or "en", duration)
