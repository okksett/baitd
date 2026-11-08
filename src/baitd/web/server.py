"""Flask app: the local UI and its JSON API.

Security (ruling R22): the server holds API keys, so every request must carry the Host
`127.0.0.1:<port>` or `localhost:<port>` of the socket it arrived on (DNS-rebinding
protection), every POST/DELETE must be `application/json` (no CSRF via simple form posts),
and no CORS headers are ever sent.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from .. import audio, captions, config, llm, tts
from ..clip import pipeline as clip_pipeline
from ..clip import sources as clip_sources
from ..clip import ytdlp_loader
from ..clip.models import FRAMINGS, ClipProfile
from ..jobqueue import JobQueue
from ..models import Profile, Script, UserError, first_sentence
from ..pipeline import STEPS, check_output_dir

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
I18N_DIR = WEB_DIR / "static" / "i18n"
UI_LANGS = ("es", "en")
THEMES = ("dark", "light")
KEY_FIELDS = ("openai_key", "gemini_key", "elevenlabs_key", "pexels_key", "pixabay_key", "compat_key")
MASK = "••••"
_MUTATING = ("POST", "PUT", "PATCH", "DELETE")
_LOCAL_HOSTS = ("127.0.0.1", "localhost")
_POSITIONS = ("bottom", "middle", "top")
_MAX_OUTLINE = 20  # px; the presets use 0-6
_LLM_PROVIDERS = tuple(llm.DEFAULT_MODELS)
_UPLOAD_PATH = "/api/uploads"
_UPLOAD_MAX = 40 * 1024**3  # long-video uploads; the global MAX_CONTENT_LENGTH stays untouched
_CHUNK = 1024 * 1024
_ID = re.compile(r"[0-9a-f]{12}")
_UNEXPECTED = "Error inesperado: {} → Revisa el registro y vuelve a intentarlo"


def _open_folder(path: Path) -> None:
    """Show a folder in the system file manager."""
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - path comes from the queue, never the client
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def mask_key(value: str) -> str:
    if not value:
        return ""
    return MASK + value[-4:] if len(value) > 8 else MASK


def _masked_settings() -> dict:
    s = config.load_settings()
    return {**s, **{k: mask_key(s.get(k, "")) for k in KEY_FIELDS}}


def _load_i18n() -> dict[str, dict[str, str]]:
    return {lang: json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8")) for lang in UI_LANGS}


def _stored_theme() -> str:
    """The saved theme, or "" when the user never chose one (the page then follows the OS)."""
    theme = config.raw_settings().get("theme")
    return theme if theme in THEMES else ""


def _cache_gb(value) -> float:
    """The clip-cache cap in GB: a positive number (or numeric string)."""
    try:
        gb = float(value) if isinstance(value, (int, float, str)) and not isinstance(value, bool) else 0.0
    except ValueError:
        gb = 0.0
    if not 0 < gb < float("inf"):
        raise UserError("Tamaño de caché no válido", "Indica un número de GB mayor que 0")
    return gb


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise UserError("La petición no es un objeto JSON válido", "Recarga la página e inténtalo de nuevo")
    return data


def _profiles() -> list[Profile]:
    """Stored profiles; the default one is created on first use so there is always one."""
    profiles = config.list_profiles()
    if not profiles:
        config.save_profile(config.default_profile())
        profiles = config.list_profiles()
    return profiles


def _find_profile(profile_id) -> Profile:
    for p in _profiles():
        if p.id == profile_id:
            return p
    raise UserError("El perfil elegido ya no existe", "Elige otro perfil arriba")


def _music_choices() -> set[str]:
    return {"none", *audio.MOODS, *(str(t["path"]) for t in audio.list_music())}


def _parse_profile(d: dict) -> Profile:
    invalid = UserError("El perfil tiene valores no válidos", "Revisa los campos del formulario")
    if not d.get("id"):
        d = {**d, "id": uuid.uuid4().hex[:8]}
    try:
        ClipProfile.from_dict(d.get("clipping", {}))
    except ValueError:
        raise UserError("Ajustes de clipping no válidos", "Revisa número de clips y duración en el perfil") from None
    try:
        p = Profile.from_dict(d)
    except (TypeError, ValueError, KeyError, AttributeError):
        raise invalid from None
    s = p.subtitle
    ok = (
        isinstance(p.name, str) and p.name.strip() and len(p.name) <= 60
        and isinstance(p.language, str) and 0 < len(p.language.strip()) <= 20
        and p.llm_provider in _LLM_PROVIDERS and p.tts_provider in tts.PROVIDERS
        and all(isinstance(v, str) for v in (p.llm_model, p.tone, p.voice, p.music))
        and -50 <= p.rate <= 50 and 0 <= p.music_volume <= 1
        and p.music in _music_choices()
        and s.preset in captions.PRESETS and s.position in _POSITIONS
        and isinstance(s.size, int) and 24 <= s.size <= 200
        and all(isinstance(c, str) and len(c) == 7 and c.startswith("#") for c in (s.color, s.highlight))
        and isinstance(s.font, str) and s.font in captions.font_families()
        and type(s.outline) is int and 0 <= s.outline <= _MAX_OUTLINE
        and isinstance(s.box, bool)
    )
    if not ok:
        raise invalid
    p.name = p.name.strip()
    p.language = p.language.strip()
    return p


def _topics(data: dict) -> list[str]:
    topics = data.get("topics")
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        raise UserError("La lista de temas no es válida", "Escribe un tema por línea")
    topics = [t.strip() for t in topics if t.strip()]
    if not topics:
        raise UserError("No hay ningún tema", "Escribe al menos un tema")
    if any(len(t) > 300 for t in topics):
        raise UserError("Un tema es demasiado largo", "Usa como máximo 300 caracteres por tema")
    return topics


def _scripts(data: dict) -> dict[str, Script]:
    invalid = UserError("Un guion revisado no es válido", "Vuelve a escribir el guion")
    raw = data.get("scripts") or {}
    if not isinstance(raw, dict):
        raise invalid
    try:
        # the hook is not edited on its own (ruling R28): it is re-derived from scene 1 below
        scripts = {topic.strip(): Script.from_dict({**s, "hook": ""}) for topic, s in raw.items()}
    except (TypeError, ValueError, KeyError, AttributeError):
        raise invalid from None
    for s in scripts.values():
        texts_ok = all(isinstance(v, str) for v in (s.title, s.description, s.language))
        scenes_ok = s.scenes and all(
            isinstance(sc.text, str) and sc.text.strip() and sc.keywords
            and all(isinstance(k, str) and k.strip() for k in sc.keywords) for sc in s.scenes)
        if not (texts_ok and scenes_ok and all(isinstance(h, str) for h in s.hashtags)):
            raise invalid
        s.hook = first_sentence(s.scenes[0].text)  # what is spoken first is the hook
    return scripts


def _is_upload(req) -> bool:
    """The one mutating request that is not JSON: raw bytes, which a cross-site page cannot send."""
    return (req.method == "POST" and req.path == _UPLOAD_PATH and req.mimetype == "application/octet-stream"
            and req.headers.get("X-Baitd-Upload") == "1")


def _bad_times() -> UserError:
    return UserError("Un clip tiene inicio o fin no válido", "Ajusta el inicio y el fin del clip")


def _public_source(src) -> dict:
    """Source info for the browser: never the local file path."""
    return {"id": src.id, "title": src.title, "channel": src.channel, "url": src.url,
            "duration": src.duration, "width": src.width, "height": src.height, "fps": src.fps,
            "has_video": src.has_video}


def _export_moments(raw, analysis: dict, duration: float) -> list[dict]:
    """The moments the user chose to export, checked against the source and the global clip limits."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise UserError("Elige entre 1 y 20 clips para exportar", "Marca al menos un momento")
    out = []
    for m in raw:
        if not isinstance(m, dict):
            raise _bad_times()
        start, end = m.get("start"), m.get("end")
        for v in (start, end):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise _bad_times()
        if (start < 0 or not clip_pipeline.MIN_CLIP_S <= end - start <= clip_pipeline.MAX_CLIP_S
                or end > duration + 1.0):
            raise _bad_times()
        framing, title, credit = m.get("framing", "cara"), m.get("title", ""), m.get("credit", "")
        if framing not in FRAMINGS or not isinstance(title, str) or not isinstance(credit, str):
            raise UserError("Un clip tiene valores no válidos", "Revisa el encuadre, el título y el crédito")
        # keep what the analysis knows about this moment (description, hashtags...) under the user's edits
        base = next((k for k in analysis["moments"] if k.get("start") == start and k.get("end") == end), {})
        out.append({**base, "start": float(start), "end": float(end), "framing": framing,
                    "title": title, "credit": credit})
    return out


def _public_job(job: dict) -> dict:
    video = Path(job["output"]) / "video.mp4" if job["output"] else None
    return {**job, "has_video": bool(video and job["status"] == "done" and video.is_file())}


def create_app(queue: JobQueue, shutdown: Callable[[], None] | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(WEB_DIR / "templates"),
                static_folder=str(WEB_DIR / "static"), static_url_path="/static")
    app.json.ensure_ascii = False
    app.json.sort_keys = False
    i18n = _load_i18n()
    voice_cache: dict[tuple[str, str], list[dict]] = {}
    voice_lock = threading.Lock()

    def job_or_404(job_id: str) -> dict:
        job = next((j for j in queue.list() if j["id"] == job_id), None)
        if job is None:
            abort(404)
        return job

    # --- R22 guards ---

    @app.before_request
    def guard():
        port = request.environ.get("SERVER_PORT", "")
        allowed = {f"{h}:{port}" for h in _LOCAL_HOSTS}
        if request.headers.get("Host", "").lower() not in allowed:
            return jsonify(error="Dirección no permitida → Abre Baitd desde la dirección que muestra la app"), 403
        if request.method == "POST" and request.path == _UPLOAD_PATH:
            if not _is_upload(request):  # the upload route takes raw bytes only, with its header
                return jsonify(error="La petición debe ser JSON → Recarga la página e inténtalo de nuevo"), 415
        elif request.method in _MUTATING and request.mimetype != "application/json":
            return jsonify(error="La petición debe ser JSON → Recarga la página e inténtalo de nuevo"), 415
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")
        return resp

    # --- errors: always {"error": "cause → fix"} ---

    @app.errorhandler(UserError)
    def user_error(e: UserError):
        return jsonify(error=str(e)), 400

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        messages = {
            404: "No se encontró lo que buscas → Recarga la página",
            403: "Acceso no permitido → Abre el vídeo desde Baitd",
            405: "Operación no permitida → Recarga la página",
        }
        msg = messages.get(e.code, f"Error {e.code} → Recarga la página e inténtalo de nuevo")
        return jsonify(error=msg), e.code

    @app.errorhandler(Exception)
    def unexpected(e: Exception):
        log.exception("Error inesperado en %s %s", request.method, request.path)
        return jsonify(error=_UNEXPECTED.format(type(e).__name__)), 500

    # --- page and assets ---

    @app.get("/")
    def index():
        settings = config.load_settings()
        lang = settings.get("ui_lang") if settings.get("ui_lang") in UI_LANGS else "es"
        strings = i18n[lang]

        def t(key: str) -> str:
            return strings.get(key) or i18n["es"].get(key) or key

        boot = {"lang": lang, "total_steps": len(STEPS), "clip_steps": len(clip_pipeline.CLIP_STEPS),
                "presets": {k: v.to_dict() for k, v in captions.PRESETS.items()}}
        return render_template("index.html", t=t, lang=lang, theme=_stored_theme(), boot=boot)

    @app.get("/fonts/<path:name>")
    def fonts(name: str):
        available = {p.name for p in captions.FONTS_DIR.glob("*.ttf") if p.is_file()}
        if name not in available:  # plain file names only: no "..", no sub-folders
            abort(404)
        return send_from_directory(captions.FONTS_DIR, name, mimetype="font/ttf", max_age=86400)

    # --- settings ---

    @app.get("/api/settings")
    def get_settings():
        return jsonify(_masked_settings())

    @app.post("/api/settings")
    def post_settings():
        data = _body()
        update = {}
        for key, value in data.items():
            if key not in config.SETTINGS_DEFAULTS:
                continue
            if key == "cache_max_gb":
                update[key] = _cache_gb(value)
                continue
            if key == "keep_sources":
                if not isinstance(value, bool):
                    raise UserError("Valor no válido para 'keep_sources'", "Revisa los ajustes")
                update[key] = value
                continue
            if not isinstance(value, str):
                raise UserError(f"Valor no válido para '{key}'", "Revisa los ajustes")
            value = value.strip()
            if key in KEY_FIELDS and value.startswith(MASK):
                continue  # the masked placeholder came back unchanged
            update[key] = value
        if "ui_lang" in update and update["ui_lang"] not in UI_LANGS:
            raise UserError("Idioma de la interfaz no válido", "Elige español o inglés")
        if "theme" in update and update["theme"] not in THEMES:
            raise UserError("Tema no válido", "Elige oscuro o claro")
        if update.get("output_dir"):  # "" = the default folder in the data dir
            check_output_dir(Path(update["output_dir"]))
        config.save_settings(update)
        with voice_lock:
            voice_cache.clear()  # a new key can change which voices are available
        return jsonify(_masked_settings())

    # --- profiles ---

    @app.get("/api/profiles")
    def get_profiles():
        return jsonify(profiles=[p.to_dict() for p in _profiles()])

    @app.post("/api/profiles")
    def post_profile():
        p = _parse_profile(_body())
        config.save_profile(p)
        return jsonify(p.to_dict())

    @app.delete("/api/profiles/<profile_id>")
    def delete_profile(profile_id: str):
        profiles = _profiles()
        if not any(p.id == profile_id for p in profiles):
            abort(404)
        if len(profiles) <= 1:
            raise UserError("No se puede borrar el último perfil", "Crea otro perfil antes de borrar este")
        config.delete_profile(profile_id)
        return jsonify(profiles=[p.to_dict() for p in _profiles()])

    # --- voices and music ---

    @app.get("/api/voices")
    def get_voices():
        provider = request.args.get("provider", "")
        lang = request.args.get("lang", "").strip().lower()
        if provider not in tts.PROVIDERS:
            raise UserError(f"Proveedor de voz desconocido: {provider}", "Elige otro en Voz")
        if not lang:
            raise UserError("Falta el idioma", "Elige un idioma para el vídeo")
        key = (provider, lang)
        with voice_lock:
            cached = voice_cache.get(key)
        if cached is None:
            cached = tts.list_voices(provider, lang, config.load_settings())
            with voice_lock:
                voice_cache[key] = cached
        return jsonify(voices=cached)

    @app.get("/api/music")
    def get_music():
        tracks = [{"id": t["id"], "mood": t["mood"], "title": t["title"], "value": str(t["path"]),
                   "bundled": t["bundled"]} for t in audio.list_music()]
        return jsonify(moods=list(audio.MOODS), tracks=tracks)

    # --- scripts and jobs ---

    @app.post("/api/script")
    def post_script():
        data = _body()
        topic = data.get("topic")
        if not isinstance(topic, str) or not topic.strip():
            raise UserError("Falta el tema", "Escribe un tema")
        profile = _find_profile(data.get("profile_id"))
        script = llm.generate_script(topic.strip(), profile, config.load_settings())
        return jsonify(script.to_dict())

    @app.post("/api/jobs")
    def post_jobs():
        data = _body()
        topics = _topics(data)
        scripts = _scripts(data)
        profile = _find_profile(data.get("profile_id"))
        return jsonify(ids=queue.add(topics, profile, scripts))

    @app.get("/api/jobs")
    def get_jobs():
        return jsonify(jobs=[_public_job(j) for j in queue.list()])

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        job_or_404(job_id)
        queue.cancel(job_id)
        return jsonify(ok=True)

    @app.delete("/api/jobs/<job_id>")
    def remove_job(job_id: str):
        job_or_404(job_id)
        queue.remove(job_id)
        return jsonify(ok=True)

    @app.post("/api/jobs/clear")
    def clear_jobs():
        queue.clear_finished()
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/video")
    def job_video(job_id: str):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":  # no embedding from other sites
            abort(403)
        job = _public_job(job_or_404(job_id))
        if not job["has_video"]:
            abort(404)
        return send_file(Path(job["output"]) / "video.mp4", mimetype="video/mp4", conditional=True)

    @app.post("/api/jobs/<job_id>/open-folder")
    def open_folder(job_id: str):
        job = job_or_404(job_id)  # the folder comes from the queue, never from the request
        folder = Path(job["output"]) if job["output"] else None
        if folder is None or not folder.is_dir():
            raise UserError("Este vídeo aún no tiene carpeta", "Espera a que termine")
        try:
            _open_folder(folder)
        except OSError:
            raise UserError("No se pudo abrir la carpeta", f"Ábrela a mano: {folder}") from None
        return jsonify(ok=True)

    # --- clipping ---

    @app.post(_UPLOAD_PATH)
    def post_upload():
        name = request.args.get("name", "")  # only a display title and an extension: never a path
        length = request.content_length
        if length is None:
            raise UserError("La subida no indica su tamaño", "Vuelve a subir el archivo desde Baitd")
        if length > _UPLOAD_MAX:
            raise UserError("El archivo es demasiado grande (máximo 40 GB)", "Recorta el video o usa uno más corto")
        if length * 1.1 > clip_sources.free_bytes(clip_sources.sources_dir()):
            raise UserError(f"No hay espacio suficiente para subir el archivo (~{length * 1.1 / 1e9:.1f} GB)",
                            "Libera espacio en el disco")
        source_id, path = clip_sources.new_upload(name)
        try:
            received = 0
            with open(path, "wb") as f:
                while True:
                    chunk = request.stream.read(_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > length:
                        break
                    f.write(chunk)
            if received != length:
                raise UserError("La subida se interrumpió", "Vuelve a subir el archivo")
            src = clip_sources.finish_upload(source_id, path, name)  # probes; removes the folder on failure
        except BaseException:
            clip_sources.delete_source(source_id)  # a broken upload leaves no partial file
            raise
        return jsonify(source_id=src.id, title=src.title, duration=src.duration, has_video=src.has_video)

    @app.post("/api/clips")
    def post_clips():
        data = _body()
        profile = _find_profile(data.get("profile_id"))
        mode = data.get("mode")
        if mode not in ("auto", "review"):
            raise UserError("Modo de clipping no válido", "Elige automático o con revisión")
        url, source_id = data.get("url"), data.get("source_id")
        if (url is None) == (source_id is None):
            raise UserError("Indica un enlace o un video subido, no ambos", "Pega un enlace o sube un archivo")
        overrides = data.get("overrides", {})
        try:
            if not isinstance(overrides, dict):
                raise ValueError("overrides must be an object")
            ClipProfile.from_dict({**profile.clipping.to_dict(), **overrides})
        except ValueError:
            raise UserError("Ajustes de clipping no válidos",
                            "Revisa número de clips y duración en el perfil") from None
        if url is not None:
            if not isinstance(url, str) or not url.strip():
                raise UserError("Ese enlace no es válido", "Pega un enlace http o https de YouTube u otro sitio")
            clip_sources.check_url(url)
            source = {"url": url.strip()}
        else:
            source = {"source_id": clip_sources.load_source(source_id).id}
        job_id = queue.add_clip_job("clip_auto" if mode == "auto" else "clip_analyze",
                                    {"source": source, "overrides": overrides}, profile)
        return jsonify(job_id=job_id)

    def analysis_or_404(analysis_id: str) -> dict:
        if not _ID.fullmatch(analysis_id) or not (clip_pipeline.analyses_dir() / f"{analysis_id}.json").is_file():
            abort(404)
        return clip_pipeline.load_analysis(analysis_id)

    @app.get("/api/analyses/<analysis_id>")
    def get_analysis(analysis_id: str):
        analysis = analysis_or_404(analysis_id)
        src = clip_sources.load_source(analysis["source_id"])
        return jsonify(source=_public_source(src), moments=analysis["moments"],
                       language=analysis["transcript"]["language"])

    @app.post("/api/analyses/<analysis_id>/export")
    def export_analysis(analysis_id: str):
        data = _body()
        analysis = analysis_or_404(analysis_id)
        src = clip_sources.load_source(analysis["source_id"])
        moments = _export_moments(data.get("moments"), analysis, src.duration)
        profile = _find_profile(data["profile_id"]) if "profile_id" in data else _profiles()[0]
        job_id = queue.add_clip_job("clip_export", {"analysis_id": analysis_id, "moments": moments}, profile)
        return jsonify(job_id=job_id)

    @app.delete("/api/analyses/<analysis_id>")
    def delete_analysis(analysis_id: str):
        if not _ID.fullmatch(analysis_id):
            abort(404)
        file = clip_pipeline.analyses_dir() / f"{analysis_id}.json"
        if not file.is_file():
            abort(404)
        try:
            src = clip_sources.load_source(json.loads(file.read_text(encoding="utf-8")).get("source_id"))
        except (OSError, ValueError, AttributeError, UserError):
            src = None  # damaged analysis or source already gone: only the analysis is removed
        if src is not None:  # the source goes too unless kept or still needed by a queued item
            clip_pipeline._release_source(src, config.load_settings(), queue.unfinished_refs)
        clip_pipeline.delete_analysis(analysis_id)
        return jsonify(ok=True)

    @app.delete("/api/sources/<source_id>")
    def delete_source(source_id: str):
        """The "Quitar archivo" button: an upload no job has used yet, never one still needed."""
        _body()
        if not clip_sources.valid_id(source_id) or not (clip_sources.sources_dir() / source_id).is_dir():
            abort(404)
        if clip_pipeline.source_needed(source_id, queue.unfinished_refs):
            return jsonify(error=str(UserError("Ese video lo usa un trabajo de la cola o un análisis",
                                               "Espera a que termine o descarta el análisis"))), 409
        clip_sources.delete_source(source_id)
        return jsonify(ok=True)

    @app.get("/api/sources/<source_id>/media")
    def source_media(source_id: str):
        if request.headers.get("Sec-Fetch-Site") == "cross-site":  # no embedding from other sites
            abort(403)
        try:
            src = clip_sources.load_source(source_id)
        except UserError:
            abort(404)
        return send_file(src.path, conditional=True)

    @app.post("/api/ytdlp/update")
    def ytdlp_update():
        _body()
        return jsonify(version=ytdlp_loader.update_ytdlp())

    @app.post("/api/shutdown")
    def post_shutdown():
        """Quit the app (the macOS .app has no console to press Ctrl+C in)."""
        def stop() -> None:
            queue.stop()  # a running job goes back to pending
            if shutdown is not None:
                shutdown()
        threading.Timer(0.3, stop).start()  # after this response has been sent
        return jsonify(ok=True)

    return app
