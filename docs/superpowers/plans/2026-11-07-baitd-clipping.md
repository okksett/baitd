# Baitd Clipping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename Baitd to Baitd and add a Clipping tab that turns a long video (an uploaded file, or a link) into ranked, vertical, captioned short clips. It has an automatic mode and a review mode.

**Architecture:** The new work goes in `src/baitd/clip/` as small modules:
- `sources` (yt-dlp download and file intake)
- `transcribe` (OpenAI whisper-1, or local faster-whisper)
- `moments` (LLM scoring over overlapping transcript windows, plus audio energy)
- `framing` (scene cuts, OpenCV YuNet face tracking, crop path)
- `render` (cut, silence removal, captions, hook title, credit, loudnorm)
- `pipeline` (auto, analyze and export jobs)

These modules reuse the existing FFmpeg runner, captions, audio mix, config and LLM clients. The existing persistent JobQueue dispatches on a new `kind` field. Network and heavy-model steps sit behind a `ClipProviders` dataclass, the same pattern as `pipeline.Providers`, so tests and `--selftest` run offline.

**Tech Stack:**
- Existing: Python ≥ 3.10, Flask, waitress 3, imageio-ffmpeg, platformdirs, fonttools, requests, pytest and PyInstaller.
- New:
  - `yt-dlp` (Unlicense)
  - `faster-whisper` (MIT, CTranslate2)
  - `opencv-python-headless` (Apache-2.0) with the YuNet face model `face_detection_yunet_2023mar.onnx` (Apache-2.0, from opencv_zoo)
  - `numpy`, as a dependency of OpenCV

**Spec:** `docs/diseno-clipping-2026-11-07.md` (Spanish; binding). It extends `docs/diseno-2026-11-07.md`. The previous plan is `docs/superpowers/plans/2026-11-07-baitd.md`. Its rulings R1–R34 are summarised in `docs/decisiones-v1.md`, and they still hold unless this plan changes them.

## Global Constraints

These are the earlier constraints, now applied to Clipping.

**Runtime and API**
- Python ≥ 3.10 on Windows, macOS (Intel and Apple Silicon) and Linux.
- The server listens on `127.0.0.1` only.
- FFmpeg comes only from `imageio_ffmpeg.get_ffmpeg_exe()`. There is no ffprobe; probe through `ffmpeg.probe`.
- R22 guards: every mutating route checks Host and accepts JSON only. The one exception is the upload route. It accepts `application/octet-stream` (not CORS-safelisted, so a cross-site page cannot send it without a preflight, which the server never answers) together with the required header `X-Baitd-Upload: 1`, and it keeps the Host check.
- API keys are never logged, never placed in URLs that are logged, and never returned by GET or shown in errors. The OpenAI key is reused for whisper-1.

**Errors and text**
- Every user-facing error is a `UserError(cause, fix)` and renders as "cause → fix". Spanish is primary. UI strings live only in `web/static/i18n/es.json` and `en.json`.

**Video output**
- Clip output is 9:16 at 1080×1920 by default (16:9 at 1920×1080 is still selectable).
- fps is 30 or 60, and never higher than the source fps.
- Constant frame rate, H.264 `yuv420p` at CRF ≈ 18 or equivalent, AAC ≥ 192 kbps at 48 kHz, `+faststart`.
- All shots are normalised to tv/BT.709 (ruling from T9).

**Audio**
- The source audio is normalised to ≈ −14 LUFS (`loudnorm=I=-14:TP=-1.5:LRA=11`).
- Clips have no music by default.
- If music is on, `audio.mix_filter` is used with the clip audio as the "voice" input. Only the music fades (R27).

**Quality**
- In Clipping, the visible crop may be upscaled up to **1.8×**, with `unsharp=5:5:0.5:5:5:0.0` applied whenever the scale is above 1.25×.
- A source whose required scale is above 1.8× falls back automatically to "completo" (full frame + blurred background). For a 9:16 crop from a landscape source, that means any source shorter than 1080 px.
- The image is never stretched.

**Captions and overlays**
- Same rules as Crear short: word-by-word, ≤ 150 ms, 1–4-word chunks, inside the safe zone.
- The hook-title overlay is the top band for the first 3.0 s. The credit sits in a corner inside the safe zone.

**Data and limits**
- User data is in `config.data_dir()`, which after Task 1 is `user_data_dir("Baitd")`, or `$BAITD_DATA`.
- Sources live in `data_dir()/fuentes/`, outside the 3 GB stock-cache cap.
- Analyses live in `data_dir()/analisis/<analysis_id>.json`.
- Whisper models live in `data_dir()/modelos/`.
- Limits: at most 4 h per source, 1–20 clips per run, clip length 10–180 s, and min ≤ max.
- TikTok, Instagram Reels and YouTube Shorts URLs are rejected with "Ese enlace es de TikTok/Reels/Shorts → Usa el video largo original".
- No cookies or login for downloads.

**Tests and process**
- Tests never touch the network or download models. Real-network tests are marked `@pytest.mark.network`.
- The full suite is `pytest -m "not network"`, and it must stay green after every task.
- `--selftest` must keep printing `SELFTEST OK <s>`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Nothing is pushed (R6).

## Review Focus

Five input classes the spec implies but no happy-path test covers. Each line names the task that pins it.

1. **A 4-hour source, or a multi-GB upload.**
   - Uploads stream to disk in chunks. Transcripts and energy curves are processed per window.
   - The whole video, the whole audio and the whole file never sit in memory.
   - Disk space is checked before download and upload. Pinned in Tasks 3 and 9.
2. **Non-ASCII and space-containing paths and titles**, e.g. a YouTube title in Japanese or emoji, or an OneDrive `Desktop\mis videos` path.
   - The yt-dlp output template uses the source id and never the title.
   - Folder names go through `config.slugify`.
   - FFmpeg receives paths as list arguments.
   - Pinned in Tasks 3 and 8.
3. **No face, many faces, or a face that leaves the frame** (screen recording, gameplay, two-person podcast).
   - With no face, framing falls back to centre.
   - The main face is chosen per shot and never flips back and forth inside a shot.
   - The crop stays inside the frame.
   - Pinned in Task 6.
4. **No usable speech** (a music video, silence, or a language the model guesses wrong).
   - The job ends with `UserError("No se encontraron momentos con habla en este video", "Prueba con otro video o elige el idioma a mano")`, not a crash and not zero-length clips.
   - Pinned in Tasks 4 and 5.
5. **Cancel or close during a long download or a local transcription.**
   - The cancel event is checked at least every 2 s in both.
   - The partial download and the work folders are removed.
   - The queue item ends `cancelled`, or goes back to `pending` on close (existing semantics).
   - Pinned in Tasks 3, 4 and 8.

---

### Task 1: Rename Baitd → Baitd

**Files:**
- Move: `src/baitd/` → `src/baitd/` (with `git mv`), and `packaging/baitd.spec` → `packaging/baitd.spec`
- Modify:
  - `pyproject.toml`
  - every `src/baitd/**/*.py` that has `baitd` imports or strings
  - `web/templates/index.html`, `web/static/app.js`, `web/static/app.css`, `i18n/es.json`, `i18n/en.json`
  - `iniciar.bat`, `iniciar.sh`, `packaging/launch.py`, `.github/workflows/build.yml`
  - `README.md`, `LICENSE` ("Baitd contributors"), `src/baitd/config.py`
  - all of `tests/`
- Test: `tests/test_config.py`, `tests/test_rename.py` (new)

**Interfaces:**
- Produces:
  - the package `baitd`, the console script `baitd = "baitd.__main__:main"`, `python -m baitd`
  - `config.data_dir()` → `$BAITD_DATA`, else `$BAITD_DATA` (legacy, so old test setups keep working), else `user_data_dir("Baitd")`
  - `config.migrate_legacy_data(old: Path, new: Path) -> bool`
  - `instance.LOCK_NAME` stays `"instance.lock"`
- Every later task imports from `baitd.*`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rename.py
import importlib, pathlib, re
def test_package_is_baitd():
    assert importlib.import_module("baitd.__main__").main
def test_no_baitd_left_in_sources():
    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {"config.py", "__main__.py"}  # legacy data-dir / env-var migration only
    hits = [p for p in (root / "src").rglob("*")
            if p.is_file() and p.suffix in {".py", ".html", ".js", ".css", ".json"} and p.name not in allowed
            and re.search(r"baitd", p.read_text(encoding="utf-8"), re.I)]
    assert hits == []
```

```python
# tests/test_config.py (add)
def test_migrate_copies_profiles_settings_keys_once(tmp_path):
    old, new = tmp_path / "Baitd", tmp_path / "Baitd"
    (old / "perfiles").mkdir(parents=True)
    (old / "settings.json").write_text('{"openai_key": "sk-x"}', encoding="utf-8")
    (old / "perfiles" / "a.json").write_text("{}", encoding="utf-8")
    assert config.migrate_legacy_data(old, new) is True
    assert (new / "settings.json").read_text(encoding="utf-8") == '{"openai_key": "sk-x"}'
    assert (new / "perfiles" / "a.json").exists()
    assert config.migrate_legacy_data(old, new) is False  # new already exists → no-op
```

Adapt the `perfiles` folder name and the file names to what `config.py` actually uses.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_rename.py tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: baitd`.

- [ ] **Step 3: Implement the rename**

- Do the `git mv` and replace every import and string.
- UI title and logo: `BAIT<em>d</em>`, same markup and CSS as the old `BAIT<em>d</em>`. Messages that named "Baitd" now say "Baitd".
- Write `migrate_legacy_data(old, new)`:
  - Copy settings, profiles and user music only when `old` exists and `new` does not.
  - Skip the queue, cache and outputs.
  - Return whether it copied anything.
- `__main__.main()` calls `migrate_legacy_data` once before taking the instance lock, with `old = Path(user_data_dir("Baitd"))`. This happens only when neither env var is set.
- In the PyInstaller spec, CI and launchers, the bundle is named "Baitd" (`Baitd.exe`, `Baitd.app`) and the artifacts are `Baitd-<os>-<arch>`.
- Translate the README brand everywhere, and add a one-line "antes se llamaba Baitd" note.

- [ ] **Step 4: Run the full suite and the selftest**

Run: `pytest -m "not network" -q` and `python -m baitd --selftest`
Expected: all tests pass (≈ 343), and the selftest prints `SELFTEST OK`.

- [ ] **Step 5: Commit**

`git commit -m "refactor: switch to the Baitd data folder"`

---

### Task 2: Clip data model and profile section

**Files:**
- Create: `src/baitd/clip/__init__.py` (empty), `src/baitd/clip/models.py`
- Modify: `src/baitd/models.py` (`Profile` gains `clipping`), `src/baitd/config.py` (`default_profile`), `src/baitd/web/server.py` (`_parse_profile` validates the new section)
- Test: `tests/test_clip_models.py`

**Interfaces:**
- Produces, in `baitd/clip/models.py`:

```python
KINDS = ("gracioso", "interesante", "polemico", "historia")       # moment types; "todos" = all
FRAMINGS = ("cara", "centro", "completo")
TRANSCRIBERS = ("openai", "local")

@dataclass
class ClipProfile:            # lives on Profile.clipping
    count: int = 5            # 1..20
    min_s: int = 20           # 10..180
    max_s: int = 60           # 10..180, >= min_s
    kind: str = "todos"       # "todos" | one of KINDS
    framing: str = "cara"     # one of FRAMINGS
    hook_title: bool = True
    credit: bool = True
    music: bool = False
    transcriber: str = "local"  # one of TRANSCRIBERS
    whisper_model: str = "small"  # "small" | "medium" | "large-v3"
    language: str = "auto"      # "auto" (detect) or an ISO code; passed to transcribe() as None when "auto"
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "ClipProfile": ...   # missing keys → defaults; bad values → ValueError

@dataclass
class SourceInfo:
    id: str            # 12-hex, also the folder name under fuentes/
    path: Path         # media file
    title: str
    channel: str       # "" for uploads
    url: str           # "" for uploads
    duration: float
    width: int         # 0 for audio-only
    height: int
    fps: float
    has_video: bool

@dataclass
class Transcript:
    language: str
    words: list[Word]  # baitd.models.Word, absolute source times
    duration: float

@dataclass
class Moment:
    start: float; end: float        # absolute source seconds, sentence-aligned
    kind: str; score: int           # 0..100
    title: str                      # hook title, ≤ 8 words
    description: str; hashtags: list[str]
    text: str                       # transcript text of the span
    framing: str = "cara"
    credit: str = ""                # user-edited credit text; "" = auto "Fuente: @<channel>"
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, d: dict) -> "Moment": ...
```

- `Profile.clipping: ClipProfile`, defaulting to `ClipProfile()`. Old profile JSON without `clipping` loads with the defaults.

- [ ] **Step 1: Write the failing tests**

```python
def test_old_profile_gets_default_clipping():
    p = Profile.from_dict({"id": "a", "name": "A"})
    assert p.clipping == ClipProfile()
def test_clip_profile_rejects_bad_values():
    for bad in ({"count": 0}, {"count": 21}, {"min_s": 9}, {"max_s": 181}, {"min_s": 60, "max_s": 30},
                {"kind": "x"}, {"framing": "x"}, {"transcriber": "x"}):
        with pytest.raises(ValueError):
            ClipProfile.from_dict(bad)
def test_moment_roundtrip():
    m = Moment(1.0, 31.5, "gracioso", 87, "Nadie esperaba esto", "d", ["#a"], "texto")
    assert Moment.from_dict(m.to_dict()) == m
def test_api_rejects_bad_clipping(client):
    r = client.post("/api/profiles", json={"id": "a", "name": "A", "clipping": {"count": 99}})
    assert r.status_code == 400 and "→" in r.get_json()["error"]
```

Reuse the `client` fixture from `tests/test_server.py`.

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_clip_models.py -q`
Expected: FAIL with `ModuleNotFoundError: baitd.clip`.

- [ ] **Step 3: Implement**

- Add the dataclasses and validation.
- `Profile.from_dict` builds `clipping` with `ClipProfile.from_dict(d.get("clipping", {}))`.
- `_parse_profile` maps `ValueError` to `UserError("Ajustes de clipping no válidos", "Revisa número de clips y duración en el perfil")`.

- [ ] **Step 4: Run the suite**

Run: `pytest -m "not network" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat: clip models and profile clipping section"`

---

### Task 3: Source intake (links, uploads, audio extraction, yt-dlp update)

**Files:**
- Create: `src/baitd/clip/sources.py`, `src/baitd/clip/ytdlp_loader.py`
- Modify: `pyproject.toml` (add `yt-dlp`), `tests/conftest.py` (media fixtures)
- Test: `tests/test_clip_sources.py`

**Interfaces:**
- Consumes: `ffmpeg.run`, `ffmpeg.probe`, `config.data_dir`, `SourceInfo`, `JobCancelled`.
- Produces:

```python
MAX_SECONDS = 4 * 3600
def sources_dir() -> Path                                   # data_dir()/fuentes, created
def check_url(url: str) -> None                             # UserError on blocked/invalid URL
def fetch_url(url: str, cancel: threading.Event, on_progress: Callable[[float], None]) -> SourceInfo
def new_upload(filename: str) -> tuple[str, Path]           # (source_id, target path); extension kept, name not used in path
def finish_upload(source_id: str, path: Path, filename: str) -> SourceInfo  # probe, enforce limits, write meta.json (title = filename stem)
def load_source(source_id: str) -> SourceInfo                # from fuentes/<id>/meta.json; UserError if gone
def delete_source(source_id: str) -> None
def extract_audio(src: SourceInfo, out: Path, cancel: threading.Event) -> Path  # 16 kHz mono WAV
def free_bytes(path: Path) -> int
# ytdlp_loader.py
def import_ytdlp():                   # returns the yt_dlp module; prefers data_dir()/yt-dlp.zip when valid
def update_ytdlp(cancel: threading.Event | None = None) -> str   # downloads latest release zip, verifies SHA2-256SUMS, returns version
```

- Test fixtures added to `conftest.py`:
  - `tmp_data`: sets `BAITD_DATA` to a tmp dir.
  - `make_mp4(seconds=10, w=1920, h=1080, fps=30)`: lavfi `testsrc2` + `sine`.
  - `make_wav(seconds=10)`.
  - `make_mp4_with_speech`: `testsrc2` + a tone gated on a known word schedule. It returns `(path, words)`.

**Rules:**
- **Blocked links.** `check_url` rejects anything that is not http(s), and these hosts and paths: `tiktok.com`, `vm.tiktok.com`, `instagram.com/reel/`, `instagram.com/reels/`, `youtube.com/shorts/`.
- **Download options.** `fetch_url` uses yt-dlp's Python API (`YoutubeDL`) with:
  - format `bv*[height<=2160]+ba/b[height<=2160]/b`, merged to mp4 with the bundled FFmpeg (`ffmpeg_location=imageio_ffmpeg.get_ffmpeg_exe()`)
  - `outtmpl` `fuentes/<id>/source.%(ext)s`, so the title is never used in a path
  - `noplaylist`, no cookies, and a `progress_hooks` callback that raises `JobCancelled` when `cancel` is set
- **Checks before downloading.** Read the info with `download=False` first.
  - Reject live streams: `UserError("Ese video es una transmisión en vivo", "Espera a que termine y usa la grabación")`.
  - Reject durations above `MAX_SECONDS`: `UserError("El video dura más de 4 horas", "Recorta el video o usa uno más corto")`.
  - Check disk space. The estimated size × 1.2 must fit, using `filesize`/`filesize_approx`, or else duration × 2 MB/s. Otherwise raise `UserError("No hay espacio suficiente para descargar el video (~N GB)", "Libera espacio en el disco")`.
- **Download errors.** yt-dlp `DownloadError` maps to `UserError("No se pudo descargar el video: <short reason, no URL>", "Actualiza yt-dlp en Ajustes o sube el archivo")`.
- **Cleanup.** On cancel or failure the `fuentes/<id>` folder is removed.
- **Uploads.** `finish_upload` probes the file and enforces `MAX_SECONDS`. Audio-only files are accepted (`has_video=False`). A file FFmpeg cannot read gives `UserError("El archivo no es un video o audio válido", "Prueba con MP4, MOV, MKV, WEBM, MP3 o WAV")`.
- **`update_ytdlp`.**
  - Download `https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp` and `SHA2-256SUMS`.
  - Verify the hash, and check that the file is a zip containing `yt_dlp/__init__.py`.
  - Write atomically to `data_dir()/yt-dlp.zip`.
  - `import_ytdlp` puts that zip first on `sys.path` before the first import. After an update, the UI says "Reinicia Baitd para usar la versión nueva".

- [ ] **Step 1: Write the failing tests** (all offline, using a fake `YoutubeDL` patched into `ytdlp_loader.import_ytdlp`)

```python
@pytest.mark.parametrize("url", ["https://www.tiktok.com/@a/video/1", "https://vm.tiktok.com/x",
    "https://www.instagram.com/reel/abc/", "https://www.youtube.com/shorts/abc"])
def test_blocked_links(url):
    with pytest.raises(UserError, match="TikTok/Reels/Shorts"):
        sources.check_url(url)
def test_fetch_uses_id_not_title_in_path(fake_ytdlp, tmp_data):
    fake_ytdlp.info = {"id": "x", "title": "日本語 🎉 / ..", "uploader": "Canal", "duration": 60, "is_live": False}
    s = sources.fetch_url("https://youtube.com/watch?v=x", threading.Event(), lambda p: None)
    assert s.path.parent.name == s.id and "日本" not in str(s.path) and s.channel == "Canal"
def test_fetch_rejects_over_4h_and_live(fake_ytdlp, tmp_data): ...
def test_fetch_checks_disk_space(fake_ytdlp, tmp_data, monkeypatch):
    monkeypatch.setattr(sources, "free_bytes", lambda p: 10)
    with pytest.raises(UserError, match="espacio"): ...
def test_cancel_during_download_removes_folder(fake_ytdlp, tmp_data): ...
def test_download_error_message_has_no_url(fake_ytdlp, tmp_data): ...
def test_upload_roundtrip_and_audio_only(tmp_data, make_wav, make_mp4): ...
def test_invalid_upload_message(tmp_data): ...
def test_extract_audio_is_16k_mono(tmp_data, make_mp4): ...
def test_update_rejects_bad_hash(tmp_data, monkeypatch): ...
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_clip_sources.py -q`
Expected: FAIL with an ImportError.

- [ ] **Step 3: Implement `sources.py` and `ytdlp_loader.py`** according to the rules above.

- [ ] **Step 4: Run the tests and the full suite**
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat: clip sources (yt-dlp links, uploads, audio, updater)"`

---

### Task 4: Transcription

**Files:**
- Create: `src/baitd/clip/transcribe.py`
- Modify: `pyproject.toml` (add `faster-whisper`)
- Test: `tests/test_clip_transcribe.py`

**Interfaces:**
- Consumes: `Transcript`, `Word`, the typed errors in `llm/_http.py`, `ffmpeg.run`, `config.data_dir`.
- Produces:

```python
def no_speech() -> UserError   # UserError("No se encontraron momentos con habla en este video", "Prueba con otro video o elige el idioma a mano")
def models_dir() -> Path        # data_dir()/modelos
def transcribe(audio: Path, provider: str, language: str | None, settings: dict, model: str,
               cancel: threading.Event, on_progress: Callable[[float], None]) -> Transcript
def openai_words(audio: Path, language: str | None, settings: dict, cancel, on_progress) -> Transcript
def local_words(audio: Path, language: str | None, model: str, cancel, on_progress) -> Transcript
```

**Rules:**
- **OpenAI path.**
  - Encode 10-minute chunks with `-ac 1 -ar 16000 -c:a libopus -b:a 24k` (≈ 1.8 MB each).
  - Post each one to `https://api.openai.com/v1/audio/transcriptions` with `model=whisper-1`, `response_format=verbose_json` and `timestamp_granularities[]=word`.
  - Offset the word times by the chunk start, and check `cancel` between chunks.
  - A missing key gives `UserError("Falta la clave de OpenAI", "Ajústala en Ajustes o usa la transcripción local")`.
  - HTTP errors go through the existing typed-error mapping, and the message never contains the key.
- **Local path.**
  - Call `faster_whisper.WhisperModel(model, device="cpu", compute_type="int8", download_root=str(models_dir()))`, then `transcribe(..., word_timestamps=True, vad_filter=True)`.
  - Iterate the segments lazily, report progress as `segment.end / duration`, and check `cancel` per segment.
  - Map a model download failure to `UserError("No se pudo descargar el modelo de transcripción", "Revisa tu conexión e inténtalo de nuevo")`.
  - Import faster-whisper lazily inside `local_words`.
- **Shared rules.**
  - Both paths strip empty words, keep punctuation in `Word.text`, sort by start, and raise `no_speech()` when fewer than 20 words remain.
  - `language=None` means auto-detect. The detected code goes into `Transcript.language`.
  - Tests inject fake HTTP responses and a fake `WhisperModel`.

- [ ] **Step 1: Write the failing tests**

```python
def test_openai_chunks_and_offsets(monkeypatch, make_wav):   # 25-min wav → 3 posts; 2nd chunk words shifted by 600 s
def test_openai_missing_key_message(make_wav):
def test_local_uses_fake_model_and_progress(monkeypatch, make_wav):
def test_no_speech_raises(monkeypatch, make_wav):
def test_cancel_between_chunks_and_segments(monkeypatch, make_wav):
def test_key_never_in_error_text(monkeypatch, make_wav):     # 401 response → message has no "sk-"
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `pytest tests/test_clip_transcribe.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** according to the rules above.

- [ ] **Step 4: Run the tests and the full suite**
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat: clip transcription (whisper-1 and local faster-whisper)"`

---

### Task 5: Moment finding

**Files:**
- Create: `src/baitd/clip/moments.py`
- Modify: `src/baitd/llm/__init__.py` (extract a reusable `complete(prompt: str, profile: Profile, settings: dict) -> str` from `generate_script`; `generate_script` then uses it)
- Test: `tests/test_clip_moments.py`

**Interfaces:**
- Consumes: `Transcript`, `Moment`, `ClipProfile`, `KINDS`, `llm.complete`, `ffmpeg.run`, `models._SENTENCE_END`, `transcribe.no_speech`.
- Produces:

```python
WINDOW_S = 480.0; OVERLAP_S = 60.0
def windows(t: Transcript) -> list[tuple[float, float]]
def build_prompt(lines: str, opts: ClipProfile, language: str) -> str
def parse_moments(raw: str, t: Transcript, opts: ClipProfile) -> list[Moment]     # ValueError on bad JSON
def snap_to_sentences(start: float, end: float, words: list[Word], min_s: float, max_s: float) -> tuple[float, float] | None
def energy_curve(audio: Path, cancel) -> list[float]       # 1 value per second, momentary loudness (LUFS) via ebur128
def energy_bonus(m: Moment, curve: list[float]) -> int      # 0..15
def find_moments(t: Transcript, audio: Path, opts: ClipProfile, profile: Profile, settings: dict,
                 want: int, cancel, on_progress, complete: Callable[[str], str] | None = None) -> list[Moment]
```

**Rules:**
- **Prompt.**
  - It is in English and gives the transcript as `[mm:ss] text` lines per sentence.
  - It asks for JSON `{"moments":[{"start":"mm:ss","end":"mm:ss","kind":...,"score":0-100,"title":"≤8 words, in <language>","description":...,"hashtags":[...]}]}`.
  - `kind` is limited to `KINDS`, filtered by `opts.kind` unless it is "todos".
  - Scoring criteria: the first sentence hooks; the clip is understandable without context; it ends on the payoff (punchline or conclusion); it fits `opts.min_s`–`opts.max_s`.
- **Bad JSON.** It gets one repair retry, the same as `generate_script`. If it is still bad, the window is skipped and a warning is logged.
- **`snap_to_sentences`.**
  - The start moves to the start of the sentence containing it.
  - The end moves to the end of the sentence containing it.
  - If the snapped length is outside [min, max], shrink by whole sentences from the end, or grow forward by whole sentences, until it fits.
  - Return `None` if impossible.
- **Scoring and ranking.**
  - Final score = LLM score + `energy_bonus`, clamped to 100.
  - Remove overlaps: keep the higher score when two moments overlap by more than 30 % of the shorter one.
  - Sort by score descending and return the top `want`.
- **Empty result.** With zero moments after all windows, raise `no_speech()`.
- **Progress.** Reported per window.

- [ ] **Step 1: Write the failing tests** (inject `complete` with a fake LLM, and use a synthetic `Transcript`)

```python
def test_windows_overlap_and_cover_all():
def test_snap_never_cuts_mid_sentence():
def test_snap_respects_min_max_or_none():
def test_dedupe_keeps_higher_score():
def test_kind_filter_and_top_n():
def test_bad_json_retried_then_window_skipped():
def test_zero_moments_raises_no_speech():
def test_energy_bonus_range(make_wav):        # loud burst gives > 0
def test_generate_script_still_uses_complete(): # existing LLM tests stay green
```

- [ ] **Step 2: Run the tests and verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the tests and the full suite**
Expected: PASS.
- [ ] **Step 5: Commit**

`git commit -m "feat: AI moment finding with sentence snapping and audio energy"`

---

### Task 6: Framing

**Files:**
- Create: `src/baitd/clip/framing.py`, `src/baitd/assets/models/face_detection_yunet_2023mar.onnx`, `src/baitd/assets/models/LICENSE-yunet.txt`
- Modify: `pyproject.toml` (add `opencv-python-headless`, `numpy`; add `assets/models/*` to package-data)
- Test: `tests/test_clip_framing.py`

**Interfaces:**
- Consumes: `SourceInfo`, `ffmpeg.run`, `ffmpeg.ffmpeg_exe`.
- Produces:

```python
MAX_UPSCALE = 1.8
@dataclass
class CropPlan:
    mode: str                         # "cara" | "centro" | "completo"
    crop_w: int; crop_h: int          # in source pixels ("completo": source size)
    keys: list[tuple[float, int]]     # (clip-relative time, crop x), piecewise-linear; "centro"/"completo": one key
    sharpen: bool                     # scale > 1.25
def required_scale(src_w: int, src_h: int, out_w: int, out_h: int) -> float
    # largest out-aspect window inside the source, scaled to out size: max(out_w/crop_w, out_h/crop_h)
def scene_cuts(src: SourceInfo, start: float, end: float, cancel) -> list[float]     # clip-relative cut times
def detect_faces(src: SourceInfo, start: float, end: float, fps: float, cancel) -> list[tuple[float, list[tuple[int, int, int, int, float]]]]
    # (clip-relative time, [(x, y, w, h, score)]) in source pixels
def plan_crop(src: SourceInfo, start: float, end: float, out_w: int, out_h: int, mode: str, cancel,
              faces=None, cuts=None) -> CropPlan
def crop_filter(plan: CropPlan, out_w: int, out_h: int) -> str     # FFmpeg filter chain from [0:v] to out_w x out_h
```

**Rules:**
- **Mode decision.** If `required_scale(...) > MAX_UPSCALE`, then `mode = "completo"`, whatever was asked. For 1080×1920 from a 1920×1080 source the scale is 1.78, so "cara" is kept. From 1280×720 it is 2.67, so the mode becomes "completo".
- **Scene cuts.** Run `ffmpeg -ss start -to end -i src -vf "scale=320:-2,select='gt(scene,0.35)',showinfo" -f null -` and parse `pts_time`.
- **Face detection.** Sample frames at 4 fps, decode them through an FFmpeg stdout pipe as raw `bgr24` at 640 px wide, and run `cv2.FaceDetectorYN` with the bundled YuNet model at score ≥ 0.6. Check `cancel` every frame.
- **Main face per shot** (between cuts): the face with the highest `area × centrality` summed over the shot. Track it by IoU ≥ 0.3 across samples, and ignore others.
- **Crop x path.**
  - Centre the main face's x in the crop with a dead zone of ±8 % of the crop width.
  - Smooth with an exponential moving average (α = 0.15 per sample).
  - Clamp to `[0, src_w − crop_w]`.
  - Reset (jump) at each scene cut. The keys hold two entries at the cut time, one old and one new.
  - A shot with no face uses the centre (first shot) or keeps the previous shot's x.
- **`crop_filter`.**
  - "cara": `crop=crop_w:crop_h:x='<piecewise-linear expr of t>':y=(ih-crop_h)/2`, then `scale=out_w:out_h:flags=lanczos`, plus `unsharp=5:5:0.5:5:5:0.0` when `sharpen` is set. If there are more than 200 keys, decimate them while keeping the jump pairs.
  - "centro": a fixed x equal to the centre.
  - "completo": `split[a][b];[a]scale=out_w:out_h:force_original_aspect_ratio=increase,crop=out_w:out_h,boxblur=20:2[bg];[b]scale=out_w:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2`.
  - Every mode ends in `out_w × out_h` exactly, with no stretching.

- [ ] **Step 1: Write the failing tests**

```python
def test_required_scale_values():                         # 1920x1080→1080x1920 ≈ 1.78; 1280x720 ≈ 2.67; 3840x2160 ≈ 0.89
def test_small_source_forces_completo():
def test_1080p_cara_sharpens_and_stays_in_frame():        # injected faces moving to the edge → all x within bounds
def test_no_faces_falls_back_to_center():
def test_jump_at_scene_cut_not_slide():                   # two keys at the cut time; no slide across it
def test_main_face_does_not_flip_within_shot():           # two faces, alternating detections → one track
def test_filters_render_exact_size(make_mp4):             # run ffmpeg with each crop_filter → probe == 1080x1920
def test_detect_faces_runs_on_synthetic_frames(make_mp4): # returns a list (may be empty), no crash, model file found
```

Unit tests inject `faces=`/`cuts=`. Only the last two run real FFmpeg/OpenCV.

- [ ] **Step 2: Run the tests and verify they fail**
- [ ] **Step 3: Implement.** Download the YuNet model once from `https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx`. Record its SHA-256 and the Apache-2.0 notice in `LICENSE-yunet.txt`, and commit both.
- [ ] **Step 4: Run the tests and the full suite**
Expected: PASS.
- [ ] **Step 5: Commit**

`git commit -m "feat: clip framing (scene cuts, YuNet face tracking, crop plans)"`

---

### Task 7: Clip render

**Files:**
- Create: `src/baitd/clip/render.py`
- Modify: `src/baitd/captions.py` (add `write_clip_ass(words, style, width, height, path, fonts_dir, top_text: str = "", top_until: float = 3.0, credit: str = "")`, which reuses the existing chunking, font, safe-zone and box helpers and adds the top-title and credit events)
- Test: `tests/test_clip_render.py`

**Interfaces:**
- Consumes: `CropPlan`, `crop_filter`, `captions.write_clip_ass`, `audio.mix_filter`, `audio.pick_track`, `audio.music_input_args`, `ffmpeg.run`, `ffmpeg.encoder_args`, `ffmpeg.pick_encoder`, `RenderOptions`, `Profile`, `SourceInfo`, `Moment`.
- Produces:

```python
SILENCE_GAP = 0.6
def keep_segments(words: list[Word], start: float, end: float, gap: float = SILENCE_GAP) -> list[tuple[float, float]]
def remap_words(words: list[Word], segments: list[tuple[float, float]]) -> list[Word]   # clip-relative times after cuts
def clip_fps(requested: int, source_fps: float) -> int     # 60 only if source >= 50, else 30
def render_clip(src: SourceInfo, m: Moment, words: list[Word], plan: CropPlan | None, profile: Profile,
                opts: RenderOptions, workdir: Path, out_path: Path, cancel, on_progress) -> Path
```

**Rules:**
- **Silence cuts.** `keep_segments` keeps speech. Any gap between consecutive words longer than `gap` is cut down to 0.15 s of silence on each side.
- **Video and audio pass.** One FFmpeg pass:
  - input `-ss <start-1> -i src`
  - trim/atrim/concat for the segments
  - then `crop_filter`
  - then the `ass` captions (copied fonts, R4/R18), with `-reinit_filter 0` and tv/BT.709 normalisation as in the existing renderer
  - audio: `loudnorm` to −14 LUFS, or `audio.mix_filter` when `profile.clipping.music` is on
- **Encoding.** Hardware-encoder failure retries with libx264 (existing behaviour). Cancel kills FFmpeg and raises `JobCancelled`.
- **Hook title.** If `profile.clipping.hook_title` is on, show `m.title` in the top band from 0 to 3.0 s, in the subtitle font, 1.1× the caption size, with a box. Uppercase it for the "poster" preset.
- **Credit.** If `profile.clipping.credit` is on, use `m.credit`, or else `"Fuente: @" + src.channel` when the channel is set. Place it bottom-left inside the safe zone, at 0.45× the caption size and 70 % opacity, for the whole clip.
- **Audio-only source** (`plan is None`). The background is the subtitle highlight colour darkened to 25 %, with `showwaves=mode=cline` in the middle third, and the captions move to "middle".

- [ ] **Step 1: Write the failing tests**

```python
def test_keep_segments_cuts_long_gaps_only():
def test_remap_words_monotonic_and_shifted():
def test_clip_fps_never_above_source():
def test_render_clip_exact_size_fps_and_duration(make_mp4_with_speech):   # 1080x1920, 30 fps, duration == sum(segments) ±1 frame
def test_render_clip_loudness_near_minus_14(make_mp4_with_speech):        # ebur128 integrated within ±1.5 LU
def test_render_audio_only_source_makes_audiogram(make_wav):
def test_hook_title_and_credit_events_in_ass(tmp_path):
def test_cancel_kills_ffmpeg(make_mp4):
```

- [ ] **Step 2: Run the tests and verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the tests and the full suite**
Expected: PASS.
- [ ] **Step 5: Commit**

`git commit -m "feat: clip render (silence cuts, crop, captions, hook title, credit, loudness)"`

---

### Task 8: Clip pipeline and queue

**Files:**
- Create: `src/baitd/clip/pipeline.py`
- Modify: `src/baitd/jobqueue.py` (items gain `kind`; dispatch to runners), `src/baitd/selftest.py` (offline clip self-test)
- Test: `tests/test_clip_pipeline.py`, `tests/test_jobqueue.py` (add cases)

**Interfaces:**
- Consumes: everything from Tasks 3–7, `pipeline.check_output_dir`, `pipeline.output_dir_error`, `config.slugify`, `ProgressFn`.
- Produces:

```python
CLIP_STEPS = ("source", "transcribe", "moments", "framing", "render")
@dataclass
class ClipProviders:          # None = real implementation
    fetch_url: Callable | None = None
    transcribe: Callable | None = None
    complete: Callable | None = None      # LLM
    detect_faces: Callable | None = None
def analyses_dir() -> Path                                      # data_dir()/analisis
def run_clip_auto(item: dict, settings: dict, cancel, progress: ProgressFn, providers: ClipProviders | None = None,
                  *, on_output=None, on_title=None) -> Path      # returns output folder
def run_clip_analyze(item: dict, settings: dict, cancel, progress, providers=None, *, on_output=None, on_title=None) -> Path
    # writes analisis/<analysis_id>.json = {"source_id","profile","transcript","moments","created_at"}; returns that file
def run_clip_export(item: dict, settings: dict, cancel, progress, providers=None, *, on_output=None, on_title=None) -> Path
def load_analysis(analysis_id: str) -> dict
def delete_analysis(analysis_id: str) -> None
# jobqueue.JobQueue
def add_clip_job(self, kind: str, payload: dict, profile: Profile) -> str
```

**Item payloads** (stored on the queue item under `payload`):
- `clip_auto` and `clip_analyze`: `{"source": {"url": str} | {"source_id": str}, "overrides": {ClipProfile fields}}`
- `clip_export`: `{"analysis_id": str, "moments": [Moment dicts with user-edited start/end/framing/title/credit]}`

**Rules:**
- **Queue changes.**
  - Existing items without `kind` are `"short"` and keep calling `run_job` exactly as today.
  - Clip kinds call their runner with `(item, settings, cancel, progress, on_output=..., on_title=...)`.
  - The public job fields gain `kind` and `analysis_id`.
  - The topic shown for clip jobs is the URL or file name until `on_title(title)` updates it to the source title.
  - Progress uses step indices 1..5 with total 5. `ProgressFn` is unchanged.
- **Output.**
  - The folder is `salidas/AAAA-MM-DD_<slugify(title)>/` (`-2`, `-3` when it exists), under the same output-dir rules and errors as `run_job`.
  - Clips are named `clip-01.mp4` and so on, in rank order.
  - `publicacion.txt` holds one block per clip: the file, the source range `HH:MM:SS–HH:MM:SS`, the title, the description and the hashtags.
  - Each clip renders in `folder/.work/clip-NN/`, which is removed afterwards.
  - On failure or cancel the whole output folder is removed (same contract as `run_job`), and `on_output` is called as soon as the folder exists.
- **Analyze.** Its item ends `done` with `analysis_id` set, which frees the queue. The transcript and candidates stay on disk, with up to 2 × count candidates.
- **Export.** If the analysis or the source is missing, it raises `UserError("El video original ya no está", "Vuelve a pegar el enlace o subir el archivo")`.
- **Source cleanup.** After a successful auto or export run, delete the source unless the settings key `keep_sources` is true (default false). An analysis whose source is gone is deleted when the export finishes or when the user discards it.
- **Cancel.** The cancel event is passed to every step.
- **Stock cache.** Clip jobs do not prune the stock cache. Sources are outside it.
- **Self-test.** `selftest.run_selftest` also runs `run_clip_auto` offline:
  - a generated 40 s 1920×1080 source
  - a fake transcriber, with words on a known schedule
  - a fake LLM that returns two moments
  - face detection that returns none

  It checks two clips at 1080×1920 and 30 fps, with durations inside `min_s..max_s`, and still prints a single `SELFTEST OK <s>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_auto_end_to_end_offline(tmp_data, make_mp4_with_speech):       # 2 clips + publicacion.txt, no .work left
def test_analyze_then_export_with_edited_bounds(tmp_data, ...):          # edited start used; analysis survives a JobQueue reload
def test_export_missing_source_message(tmp_data):
def test_cancel_removes_folder_and_partial_source(tmp_data, ...):
def test_source_deleted_after_export_unless_keep(tmp_data, ...):
def test_queue_old_items_default_to_short(tmp_path):                     # queue.json without kind still runs run_job
def test_queue_clip_kinds_dispatch_and_persist(tmp_path):
def test_running_clip_item_partial_folder_cleaned_on_load(tmp_path):     # reuses the I5 semantics
def test_selftest_includes_clips():
```

- [ ] **Step 2: Run the tests and verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the full suite and the selftest**
Expected: PASS, and `SELFTEST OK`.
- [ ] **Step 5: Commit**

`git commit -m "feat: clip pipeline (auto, analyze, export) on the persistent queue"`

---

### Task 9: Server API for Clipping

**Files:**
- Modify: `src/baitd/web/server.py`
- Test: `tests/test_server_clip.py`

**Interfaces:**
- Consumes: `sources.*`, `clip.pipeline.load_analysis`/`delete_analysis`, `JobQueue.add_clip_job`, `ytdlp_loader.update_ytdlp`.
- Produces these routes. Errors always use `{"error": "cause → fix"}`.

| Method | Route | Body / params | Result |
|---|---|---|---|
| POST | `/api/uploads?name=<filename>` | raw bytes; `Content-Type: application/octet-stream`; header `X-Baitd-Upload: 1` | `{"source_id","title","duration","has_video"}` |
| POST | `/api/clips` | `{"profile_id","mode":"auto"\|"review","url"?,"source_id"?,"overrides"?}` | `{"job_id"}` |
| GET | `/api/analyses/<id>` | none | `{"source":{...},"moments":[...],"language"}` |
| POST | `/api/analyses/<id>/export` | `{"moments":[{start,end,framing,title,credit}]}` | `{"job_id"}` |
| DELETE | `/api/analyses/<id>` | none | `{"ok":true}`; 404 if unknown |
| GET | `/api/sources/<id>/media` | HTTP Range | the source file, for previews |
| POST | `/api/ytdlp/update` | `{}` | `{"version"}` |

**Rules:**
- **Uploads.**
  - The upload route is the only mutating route exempt from the JSON-only check, and the exemption is decided in the `guard` before-request.
  - It still requires the Host check, `application/octet-stream` and `X-Baitd-Upload: 1`. A missing or different header gives 415 with the existing JSON message.
  - Stream `request.stream` to the target in 1 MiB chunks. Never call `request.get_data()`.
  - Check free space against `Content-Length` × 1.1 before writing.
  - Allow up to 40 GB on this route only. Leave the global `MAX_CONTENT_LENGTH` as it is and check `Content-Length` inside the route.
  - A broken upload removes the partial file.
- **`/api/clips`.** Call `sources.check_url(url)` before queueing. `url` and `source_id` are mutually exclusive, and exactly one is required.
- **Exports.** Start and end are validated against the source duration and `min_s..max_s`. Bad values give `UserError("Un clip tiene inicio o fin no válido", "Ajusta el inicio y el fin del clip")`.
- **`/api/sources/<id>/media`.** Use `send_file(..., conditional=True)` so Range works. Reject `Sec-Fetch-Site: cross-site` with 403 (same as the video route, M16).
- **Settings.** The settings POST accepts `keep_sources: bool`.

- [ ] **Step 1: Write the failing tests**

```python
def test_upload_requires_octet_stream_and_header(client):     # multipart → 415; json → 415; missing header → 415
def test_upload_rejects_foreign_host(client):
def test_upload_streams_and_probes(client, make_mp4):
def test_upload_no_space(client, monkeypatch):
def test_clips_auto_and_review_create_jobs(client, monkeypatch):
def test_clips_rejects_blocked_url_and_bad_source_args(client):
def test_analysis_get_export_validation_delete(client, tmp_data):
def test_source_media_supports_range(client, make_mp4):       # Range: bytes=0-99 → 206, 100 bytes
def test_source_media_rejects_cross_site(client, make_mp4):
def test_ytdlp_update_route(client, monkeypatch):
```

- [ ] **Step 2: Run the tests and verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run the tests and the full suite**
Expected: PASS.
- [ ] **Step 5: Commit**

`git commit -m "feat: clipping API (uploads, clip jobs, analyses, previews, yt-dlp update)"`

---

### Task 10: Clipping UI

**Files:**
- Modify: `src/baitd/web/templates/index.html`, `src/baitd/web/static/app.js`, `src/baitd/web/static/app.css`, `src/baitd/web/static/i18n/es.json`, `src/baitd/web/static/i18n/en.json`
- Test: `tests/test_server.py` (template keys), `tests/test_i18n.py` (es/en key parity; create it if missing)

**Interfaces:**
- Consumes: the Task 9 routes, plus the existing `/api/jobs`, `/api/profiles` and `/api/settings`.

**Rules** (visual reference: `docs/mockups/ui-aprobada.html`; same tokens, fonts and dark/light gradients):
- **Tabs.** Two tabs sit next to the logo: "Crear short" and "Clipping". The selected tab is remembered in `localStorage`, wrapped in try/catch. Each tab has its own left panel, and the right column (preview, progress and queue) is shared.
- **Left panel of the Clipping tab:**
  - A drop zone, "Suelta un archivo o pega un enlace". It holds a URL input, a file button and a drag-and-drop area. Uploads use `XMLHttpRequest`, show their percentage and can be cancelled with `abort()`.
  - A row with clips (1–20), duration min–max and moment type.
  - Accordions:
    - **Transcripción**: OpenAI or local, with a "Local: más lenta, gratis" hint and a model select.
    - **Encuadre**: cara, centro or completo.
    - **Subtítulos**: the same controls as Crear short, sharing the profile's `subtitle`.
    - **Extras**: título gancho, crédito and música.
  - The buttons "Buscar momentos" (secondary) and "Crear clips" (primary pill).
  - The one-line rights notice: es "Si usas videos de otros canales, respeta sus derechos y las reglas de cada plataforma."; en "If you use other channels' videos, respect their rights and each platform's rules."
- **Review list.** It appears when an analyze job is done. Each candidate card shows:
  - a `<video>` preview of `/api/sources/<id>/media#t=start,end` that pauses at `end`
  - score, type and title (editable)
  - start and end steppers (±0.5 s, plus typed mm:ss)
  - a framing select and a credit text field
  - a checkbox, checked by default for the top `count`

  The buttons are "Exportar seleccionados" and "Descartar análisis". Removing every card discards the analysis.
- **Busy state.** Disable the profile select and the run buttons, as in Crear short.
- **Profile settings.** Changes in this tab save to `profile.clipping` with the existing chained, debounced save (M9).
- **Settings dialog.** It gains "Guardar videos originales" (`keep_sources`) and "Actualizar yt-dlp" (shows the version, or the error, plus the restart note).
- **i18n.** Every new string is in es and en with identical key sets.

- [ ] **Step 1: Write the failing tests**

```python
def test_i18n_key_parity():                         # es.json keys == en.json keys
def test_index_has_clipping_tab_and_drop_zone(client):
def test_index_has_rights_notice_key(client):
```

- [ ] **Step 2: Run the tests and verify they fail**
- [ ] **Step 3: Implement the UI**
- [ ] **Step 4: Run the suite, then check visually**

Run: `pytest -m "not network" -q`. Then start `python -m baitd --no-browser --port 18765` with `BAITD_DATA` pointing to a scratch folder. Open it in the browser pane and take screenshots of both tabs in dark and light at 1280 px and 390 px. The console must show no errors.
Expected: PASS. The screenshots match the mockup style.

- [ ] **Step 5: Commit**

`git commit -m "feat: clipping tab UI with review list"`

---

### Task 11: Packaging, CI and docs

**Files:**
- Modify: `packaging/baitd.spec`, `.github/workflows/build.yml`, `README.md`, `pyproject.toml` (version `0.2.0`)
- Test: local frozen build on Windows (verification steps below)

**Rules:**
- **Bundle contents.**
  - The spec collects `yt_dlp` (`collect_submodules`), `faster_whisper`, the `ctranslate2` binaries (`collect_dynamic_libs("ctranslate2")`), `cv2` (headless), `numpy`, the YuNet model and its license, and the existing fonts, music, web and FFmpeg.
  - No Whisper model is bundled.
  - Exclude `tkinter`, Qt and `torch` if any hook pulls them in.
- **CI** (same 4-OS matrix):
  - runs `--selftest` on the built app;
  - fails if the zipped artifact is larger than 450 MB, which leaves room above the 400 MB target;
  - uploads `Baitd-<os>-<arch>`.
- **README** (ES and EN):
  - a Clipping section covering links and uploads, local versus OpenAI transcription (with the time estimates from spec §5), the first-run model download (~250 MB), the three framing modes, and where sources and analyses live;
  - the rights notice;
  - updating yt-dlp from Ajustes.

- [ ] **Step 1: Build locally**

Build outside OneDrive, under `%TEMP%\baitd-build`, with `pyinstaller packaging/baitd.spec`.

- [ ] **Step 2: Verify the frozen app**

- `Baitd.exe --selftest` prints `SELFTEST OK`.
- `Baitd.exe --no-browser --port 18765` serves GET / with 200.
- The zipped bundle is under 450 MB. Report the size.

- [ ] **Step 3: Update CI and the README**

- [ ] **Step 4: Run the full suite**
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "build: bundle clipping deps, CI size gate, README clipping section"`
