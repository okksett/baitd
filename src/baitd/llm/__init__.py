from __future__ import annotations

import json
import logging
import re
import threading

from ..models import Profile, Scene, Script, UserError, first_sentence
from . import gemini, openai_compat

__all__ = ["generate_script", "build_prompt", "parse_script", "complete"]

log = logging.getLogger(__name__)

DEFAULT_MODELS = {"openai": "gpt-4o-mini", "gemini": "gemini-3.6-flash", "compat": "llama3.1"}
WORDS_PER_SECOND = 2.5
HOOK_MAX_WORDS = 8  # asked for in the prompt: under 3 seconds spoken
HOOK_REJECT_WORDS = 10  # longer hooks are sent back once for repair (ruling R29)

_SCHEMA = (
    '{"title": str, "hook": str, "scenes": [{"text": str, "keywords": [str, str]}], '
    '"description": str, "hashtags": [str]}'
)


def build_prompt(topic: str, profile: Profile) -> str:
    words = round(profile.seconds * WORDS_PER_SECOND)
    lo, hi = round(words * 0.85), round(words * 1.15)
    lines = [
        "You write voice-over scripts for vertical short videos.",
        f"Topic: {topic}",
        f"Language of all spoken text, title, description and hashtags: {profile.language} "
        "(ISO code or language name). Write the script in that language.",
        f"Total length: about {words} words (between {lo} and {hi}) for {profile.seconds} seconds of narration.",
        "Structure: 5 to 9 scenes, each with 1 or 2 short sentences.",
        "The hook is the first sentence of the first scene: a question, a surprising fact or a promise. "
        f"The hook is at most {HOOK_MAX_WORDS} words (under 3 seconds spoken). "
        "Repeat it exactly in the \"hook\" field.",
        "For each scene give \"keywords\": 2 or 3 concrete, visual search terms in English "
        "(things a camera can film), regardless of the script language.",
    ]
    if profile.tone.strip():
        lines.append(f"Tone: {profile.tone.strip()}")
    lines += [
        "Also give a short title, a one-sentence description and 3 to 6 hashtags.",
        f"Reply with JSON only, no commentary, matching this schema: {_SCHEMA}",
    ]
    return "\n".join(lines)


def _strip_fences(raw: str) -> str:
    return re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE)


def _nonempty_str(obj: dict, key: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"'{key}' is missing or empty")
    return v.strip()


def parse_script(raw: str, language: str, strict_hook: bool = True) -> Script:
    """Validate the LLM reply. The hook is the first sentence of scene 1 (whatever the "hook"
    field says); with strict_hook a hook over HOOK_REJECT_WORDS words is rejected (ValueError)."""
    text = _strip_fences(raw)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in the reply")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("the JSON root must be an object")

    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValueError("'scenes' must be a non-empty list")
    scenes = []
    for i, s in enumerate(raw_scenes, 1):
        if not isinstance(s, dict):
            raise ValueError(f"scene {i} must be an object")
        kws = s.get("keywords")
        if (not isinstance(kws, list) or not kws
                or not all(isinstance(k, str) and k.strip() for k in kws)):
            raise ValueError(f"scene {i} needs non-empty 'keywords'")
        scenes.append(Scene(text=_nonempty_str(s, "text"), keywords=[k.strip() for k in kws]))

    tags = data.get("hashtags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("'hashtags' must be a list of strings")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError("'description' must be a string")
    hook = first_sentence(scenes[0].text)
    n = len(hook.split())
    if strict_hook and n > HOOK_REJECT_WORDS:
        raise ValueError(f"the hook (the first sentence of the first scene) has {n} words; "
                         f"it must have at most {HOOK_MAX_WORDS}")
    return Script(
        title=_nonempty_str(data, "title"), hook=hook, scenes=scenes,
        description=description.strip(), hashtags=[t.strip() for t in tags if t.strip()],
        language=language,
    )


def complete(prompt: str, profile: Profile, settings: dict, cancel: threading.Event | None = None) -> str:
    """One provider call (OpenAI, Gemini or OpenAI-compatible) returning the raw reply text."""
    provider = profile.llm_provider
    if provider not in DEFAULT_MODELS:
        raise UserError(f"Proveedor de IA desconocido: '{provider}'", "Elige OpenAI, Gemini o compatible en el perfil")
    model = profile.llm_model.strip() or DEFAULT_MODELS[provider]
    if provider == "gemini":
        return gemini.complete(prompt, model, settings, cancel=cancel)
    return openai_compat.complete(prompt, model, settings, provider=provider, cancel=cancel)


def generate_script(topic: str, profile: Profile, settings: dict) -> Script:
    def _complete(prompt: str) -> str:
        return complete(prompt, profile, settings)

    prompt = build_prompt(topic, profile)
    try:
        script = parse_script(_complete(prompt), profile.language)
    except ValueError as first:
        retry = f"{prompt}\n\nReturn only valid JSON matching the schema. Your previous reply was rejected: {first}"
        try:
            script = parse_script(_complete(retry), profile.language, strict_hook=False)
        except ValueError:
            raise UserError("La IA no devolvió un guion válido", "Inténtalo de nuevo o cambia de modelo") from None
    n = len(script.hook.split())
    if n > HOOK_REJECT_WORDS:  # a long hook is worse than none, but a failed batch is worse still
        log.warning("El gancho sigue siendo largo tras reintentar (%d palabras); se acepta", n)
    return script
