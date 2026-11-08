from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import ffmpeg
from ..models import Profile, Script, UserError, Voiceover, Word
from . import edge, elevenlabs, openai_tts
from .timing import align_to_script, estimate_words

__all__ = ["synthesize", "list_voices", "PROVIDERS"]

SAMPLE_RATE = 48000

# provider -> synth_scene(text, profile, settings, out_path) -> word timings (scene-local seconds) or None
PROVIDERS: dict[str, Callable[[str, Profile, dict, Path], list[Word] | None]] = {
    "edge": edge.synth_scene,
    "openai": openai_tts.synth_scene,
    "elevenlabs": elevenlabs.synth_scene,
}
_KEYS = {"openai": ("openai_key", "OpenAI"), "elevenlabs": ("elevenlabs_key", "ElevenLabs")}
# ElevenLabs alignment already carries the script's punctuation; the others need align_to_script.
_PUNCTUATED = {"elevenlabs"}


def _require_key(provider: str, settings: dict) -> None:
    if provider in _KEYS:
        key, label = _KEYS[provider]
        if not settings.get(key):
            raise UserError(f"Falta la clave de {label}", "Añádela en Ajustes")


def synthesize(script: Script, profile: Profile, settings: dict, workdir: Path) -> Voiceover:
    """One TTS call per scene, joined into workdir/voice.wav (48 kHz) with exact scene bounds."""
    provider = profile.tts_provider
    if provider not in PROVIDERS:
        raise UserError(f"Proveedor de voz desconocido: {provider}", "Elige otro en el perfil")
    _require_key(provider, settings)
    workdir.mkdir(parents=True, exist_ok=True)

    wavs: list[Path] = []
    words: list[Word] = []
    bounds: list[tuple[float, float]] = []
    offset = 0.0
    for i, scene in enumerate(script.scenes):
        raw = workdir / f"voice_{i}.mp3"
        wav = workdir / f"voice_{i}.wav"
        timed = PROVIDERS[provider](scene.text, profile, settings, raw)
        try:
            ffmpeg.run(["-i", str(raw), "-vn", "-ar", str(SAMPLE_RATE), "-ac", "2", str(wav)])
            dur = ffmpeg.probe(wav)["duration"]
        except RuntimeError:
            raise UserError("No se pudo procesar el audio de la voz", "Prueba otra voz o inténtalo de nuevo") from None
        tokens = scene.text.split()
        if timed and provider in _PUNCTUATED and len(timed) == len(tokens):
            local = timed
        elif timed:
            local = align_to_script(tokens, timed, span=(0.0, dur))
        else:
            local = estimate_words(scene.text, 0.0, dur)
        words += [Word(w.text, min(w.start, dur) + offset, min(w.end, dur) + offset) for w in local]
        bounds.append((offset, offset + dur))
        offset += dur
        wavs.append(wav)

    listing = workdir / "voice_list.txt"
    listing.write_text("".join(f"file '{w.name}'\n" for w in wavs), encoding="utf-8")
    out = workdir / "voice.wav"
    try:
        ffmpeg.run(["-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", out.name], cwd=workdir)
    except RuntimeError:
        raise UserError("No se pudo unir el audio de la voz", "Inténtalo de nuevo") from None
    total = ffmpeg.probe(out)["duration"]
    if bounds:
        bounds[-1] = (bounds[-1][0], total)
    return Voiceover(path=out, duration=total, words=words, scene_bounds=bounds)


def list_voices(provider: str, language: str, settings: dict) -> list[dict]:
    """Voices as {id, name, gender, free}."""
    if provider == "edge":
        return edge.list_voices(language, settings)
    if provider == "openai":
        return openai_tts.list_voices(language, settings)
    if provider == "elevenlabs":
        _require_key(provider, settings)
        return elevenlabs.list_voices(language, settings)
    raise UserError(f"Proveedor de voz desconocido: {provider}", "Elige otro en el perfil")
