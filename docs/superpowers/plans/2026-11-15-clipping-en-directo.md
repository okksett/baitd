# Baitd 0.5.0: Clipping en directo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new "En directo" tab inside Clipping.
- It keeps the last 5 minutes of a Kick, Twitch or YouTube live stream.
- On the user's own channel, a global hotkey (F9 by default) marks moments.
- On other people's channels, Baitd proposes moments of humor or controversy by itself.
- Every moment lands in a review list and exports through the existing Clipping pipeline.
- New in that pipeline: a "Partido" layout (cam on top, game below) and a platform-logo credit.

**Architecture:** A new package `src/baitd/live/` holds the work, one focused module per job:
- the HLS playlist parser, the segment ring buffer and the capture thread;
- the mark logic, the chat readers and the detector signals;
- the AI prompt, the hotkey and a `LiveManager` that ties them together.

How it connects to what already exists:
- **Each saved moment** becomes an ordinary source (`fuentes/<id>/video.mp4`) plus an ordinary analysis (`analisis/<id>.json`) with `"origin": "live"`.
- **The existing review card and `POST /api/analyses/<id>/export`** do the editing and the export.
- **The export job** learns to transcribe when the analysis has no words yet.
- **Flask routes under `/api/live/*`** drive the new tab.

**Tech Stack:** Python 3.10+, Flask, waitress, requests, yt-dlp, aiohttp (websockets for chat), ctypes (Windows hotkey), pynput (macOS/Linux hotkey), FFmpeg, faster-whisper, vanilla JS, pytest, PyInstaller.

**Spec:** `docs/diseno-directo-2026-11-15.md` (approved by Mauricio on 2026-11-15). Read it before any task: it is the authority, and this plan argues from it.

## Facts measured on 2026-11-15 (the plan relies on them)

- **Kick:**
  - `kick.com/api/v2/channels/<slug>` answers plain `requests` with a browser User-Agent. `livestream` is `null` when the channel is offline.
  - Live channels are listed at `https://kick.com/stream/featured-livestreams/es`, under `data[].channel.slug`.
  - yt-dlp gives media playlists up to `1080p60`, with 4.17 s segments whose `EXTINF` title is `live`.
  - Each segment has an `EXT-X-PROGRAM-DATE-TIME`, the playlist has `EXT-X-DATERANGE ... X-SERVER-TIME="<epoch>"` and `EXT-X-PREFETCH` lines, and the segments are MPEG-TS.
  - Our own reader polled for 24 s, downloaded 13 segments and concatenated them with `-c copy` into a playable 54 s h264+aac MP4.
  - The newest program-date-time on Kick's playlist was **4.5 s ahead of this PC's clock**.
- **Kick chat:**
  - The old Pusher key `32cbd69e4b950bf97679` is dead: it returns error 4001.
  - What works now: `POST https://web.kick.com/api/v1/realtime/auth/connection` with `{"client_id": <uuid4>}`, which returns `data.token` (a guest JWT, about 45 min).
  - Then connect to `wss://realtime.us-east-1.platform.kick.com/connection/websocket` with the header `Origin: https://kick.com`.
  - Send `{"id":1,"connect":{"token":T,"name":"js"}}`, then `{"id":2,"subscribe":{"channel":"chatrooms.<chatroom_id>.v2"}}`.
  - Pushes then carry `push.pub.data = {"event":"App\\Events\\ChatMessageEvent","data":"<json string with content>"}`. Reply `{}` to `{}` pings.
  - `POST https://web.kick.com/api/v1/realtime/connection`, sent with `{"client":{"id":..,"type":"web"},"capabilities":{"accepted_providers":[{"provider":"centrifugo"}]}}`, returns that websocket URL.
  - The test got 6 messages in 30 s on a channel with 1,261 viewers.
- **Twitch:**
  - The 1080p source format is a media playlist.
  - During an ad, segments have `EXTINF:<d>,Amazon|<id>` and `EXT-X-DATERANGE CLASS="twitch-stitched-ad"`; live segments are titled `live`. The playlist also carries program-date-times.
- **YouTube:**
  - The live HLS format is a media playlist with about 720 segments (a 1 h window), one program-date-time at the top and empty `EXTINF` titles.
  - `EXT-X-DATERANGE CLASS="CUEPOINT-AD"` marks are cue points only: the video carries no ads.
- **Python packages:** `aiohttp` 3.14.5 is already installed as a dependency of edge-tts. Neither `websockets` nor `pynput` is installed.

## User rulings (2026-11-15)

- The capture starts only when the user presses "Capturar". It never starts by itself, and closing the window stops it.
- Kick is mandatory in the first version.
- Manual marks get no AI and no transcription during capture: the AI title is on demand in review, and the transcription happens at export.

## Controller rulings (recorded here so the implementer does not re-decide)

- **R1 · Moment storage:**
  - A moment is one source plus one analysis, and the analysis has `"origin": "live"` and a `"live"` block.
  - The review card is the existing one: "Revisar" opens it on that analysis.
  - Exporting several moments queues one `clip_export` job per moment.
- **R2 · Review padding:** a saved moment's MP4 holds the mark plus up to `REVIEW_PAD_S = 20` s on each side (whatever the buffer has), so review can widen it.
- **R3 · Start of capture:** the first playlist poll downloads only the newest `FIRST_FETCH_S = 60` s, not the whole buffer, so the start does not burst 200+ MB on the streaming PC.
- **R4 · Hotkey lifetime:**
  - The hotkey is registered while Baitd runs **and at least one channel is saved**. So a press without a capture can be explained in the window, as spec §6.1 asks.
  - It is unregistered when the last channel is removed and on exit.
  - Windows uses `MOD_NOREPEAT`: holding the key down counts as one press.
- **R5 · "Now":**
  - **Own channel:** "now" is `server_now()`, which is the local clock plus the server-clock offset. The program-date-time is the capture time at the platform, so this lines up with what the streamer saw.
  - **Other channels:** "now" is the buffer head.
  - **A playlist without program-date-times** gets synthetic segment times equal to arrival time minus the channel's `delay_s`, or `DEFAULT_DELAY_S = {"kick": 8, "twitch": 6, "youtube": 15}` when `delay_s` is unset.
  - `delay_s` is used only in that case.
- **R6 · Audio signal:** only loudness bursts. Laughter in the audio is left to the AI (stage 3). The closing notes say this.
- **R7 · Partido game crop:** the game crop is the centre of the frame. "The centre of the action" is not attempted.
- **R8 · Credit on others' clips:**
  - The moment stores `credit="@<handle>"` and `badge="<platform>"`.
  - The credit always shows, as a typed credit does today. The badge comes from the analysis, never from the browser.
- **R9 · Kind of a manual or unclassified moment:** `"interesante"`. Its review card shows the reason ("Lo marcaste tú", "El chat explotó", …) instead of a score.
- **R10 · fMP4 playlists** (`EXT-X-MAP`) are not supported: the capture stops with "Este directo usa un formato que Baitd aún no lee". None of the three platforms uses fMP4 today.

## Global Constraints

- **i18n:** every visible string in `index.html` goes through i18n, with a `data-i18n` attribute and a key in **both** `es.json` and `en.json`.
  - `tests/test_server.py::test_index_has_no_hardcoded_copy` and `test_js_uses_known_i18n_keys_only` enforce this.
  - Insert new JSON keys as text lines; never re-dump the JSON files.
  - New keys use the prefix `live.`.
- **Error messages:** `UserError(cause, fix)` text is Spanish, in plain words, with no local paths and no URLs that carry query strings.
- **API keys and tokens** (AI keys, Kick guest tokens, playlist tokens) never appear in logs, GET responses or error text.
  - Log a playlist URL only as `scheme://host/…`.
- **No accounts and no sign-in:** read streams and chats only as an anonymous viewer.
- **Tests never touch the network.** Real-network tests are marked `@pytest.mark.network`.
  - Run from the repo root: `python -m pytest -m "not network" -q -p no:cacheprovider`.
  - Use the venv Python at `C:\Users\zacli\AppData\Local\Temp\claude\46a585e4df8b1555\b9b081ba-18dd-4507-aa5f-a4a92af6859e\scratchpad\venv\Scripts\python.exe`.
- **Light on the streaming PC:**
  - Capture never re-encodes.
  - Every FFmpeg call made during a capture runs with `low_priority=True` (Task 2).
  - Live transcription runs in a below-normal-priority thread with `cpu_threads=2`.
- **Spec values (exact):**
  - `BUFFER_S = 300`
  - `live_before_s = 45` and `live_after_s = 15`, both editable
  - `EXTEND_S = 15` and `MAX_MARK_S = 180`
  - `live_hotkey = "F9"`
  - `live_ai_per_hour = 6`
  - `live_max_height = 1080`, with choices `(720, 1080)`
  - candidate window of about 60 s: `CANDIDATE_BEFORE_S = 40`, `CANDIDATE_AFTER_S = 20`
- **Dependencies:**
  - add `"aiohttp>=3.9,<4"` and `"pynput>=1.7.6,<2; sys_platform != 'win32'"` to `pyproject.toml`;
  - no other new runtime dependency;
  - the frozen app stays under the 450 MB CI size gate.
- **Off limits:** never touch `.impeccable/` (untracked). Never `git push`; the controller pushes.
- **Commits:** every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Use `git commit -F <file>`: PowerShell 5.1 mangles here-strings passed to git.

## Review Focus

1. **This PC's clock is wrong by seconds.** It was 4.5 s off against Kick on 2026-11-15.
   - A manual mark must cover what the streamer actually did, so the offset comes from `X-SERVER-TIME` or the HTTP `Date` header, never from the raw local clock.
   - Tests: Task 5 `test_offset_from_server_time` and Task 7 `test_manual_mark_uses_server_time`.
2. **The stream ends or the network drops** while a mark is still recording its 15 s after.
   - The mark is saved with what arrived if that is at least 10 s, otherwise the window says it was too short. A mark is never lost silently.
   - Test: Task 7 `test_stream_end_seals_pending_mark`.
3. **Holding or hammering the hotkey.**
   - Holding F9 makes one press (`MOD_NOREPEAT`).
   - Ten fast presses make one moment capped at 3 minutes, not ten moments.
   - Tests: Task 6 `test_presses_extend_and_cap` and Task 8 `test_register_uses_norepeat`.
4. **Twitch ads:**
   - The preroll when capture starts, or an ad in the middle of a mark, must never reach a clip.
   - A moment that skipped an ad is flagged so review warns.
   - Tests: Task 1 `test_twitch_ad_segments`, Task 2 `test_cut_across_a_gap` and Task 5 `test_ads_never_enter_the_buffer`.
5. **Baitd closes or crashes mid-capture:**
   - Saved moments survive the startup sweep.
   - The stale buffer folder is deleted at the next start.
   - A normal close seals the pending mark.
   - Tests: Task 3 `test_sweep_keeps_live_analyses` and Task 7 `test_close_seals_and_clears_buffer`.

---

### Task 1: HLS media-playlist parser

**Files:**
- Create: `src/baitd/live/__init__.py` (empty), `src/baitd/live/hls.py`
- Test: `tests/test_live_hls.py`

**Interfaces:**
- Produces:
  - `hls.Segment(seq: int, url: str, duration: float, start: float | None, ad: bool)`, a frozen dataclass. `start` is epoch seconds.
  - `hls.MediaPlaylist(segments: list[Segment], target_duration: float, ended: bool, server_time: float | None, has_map: bool)`.
  - `hls.parse_media_playlist(text: str, base_url: str) -> MediaPlaylist`.
  - `hls.is_master(text: str) -> bool`.

**Rules:**
- **Segment URLs:** relative URLs are resolved against `base_url` with `urljoin`.
- **`EXT-X-PREFETCH` lines** are ignored: those segments are not ready yet.
- **Sequence numbers:** `seq` starts at `EXT-X-MEDIA-SEQUENCE` (0 if absent).
- **Segment start:**
  - a segment preceded by its own `EXT-X-PROGRAM-DATE-TIME` takes that time;
  - otherwise it takes `previous.start + previous.duration`;
  - with no program-date-time anywhere, `start` is `None`.
- **Ads:** `ad=True` when either rule matches:
  - the `EXTINF` title is non-empty and does not start with `live`;
  - the segment's start falls inside an `EXT-X-DATERANGE` with `CLASS="twitch-stitched-ad"`, from `START-DATE` to `START-DATE + DURATION`.
- **`server_time`:** the largest `X-SERVER-TIME` attribute of any DATERANGE.
- **`ended`:** set when the playlist has `EXT-X-ENDLIST`.
- **`has_map`:** set when the playlist has `EXT-X-MAP`.

- [ ] **Step 1: Write the failing tests.** Each test uses a small inline playlist string modelled on the facts above.
  - `test_kick_playlist`:
    - the playlist has two `live` segments with program-date-times `2026-11-15T15:47:49.328Z` and `…53.495Z`, a DATERANGE with `X-SERVER-TIME="1794757701.19"`, and one `#EXT-X-PREFETCH:https://x/p.ts` line;
    - expect `len(segments) == 2`;
    - expect `segments[0].start == datetime(2026,11,15,15,47,49,328000,tzinfo=utc).timestamp()`;
    - expect `segments[1].seq == segments[0].seq + 1`, `server_time == 1794757701.19` and `not any(s.ad for s in segments)`.
  - `test_twitch_ad_segments`:
    - one `#EXTINF:2.000,Amazon|2474283100495.0` segment, then a stitched-ad DATERANGE with `START-DATE` equal to the next segment's program-date-time and `DURATION=4.0`, then that segment titled `live`, then a later `live` segment;
    - expect `[s.ad for s in segments] == [True, True, False]`.
  - `test_youtube_playlist_chains_times`:
    - one program-date-time, then three `#EXTINF:5.0,` segments;
    - expect the starts to be `t, t+5, t+10` and no segment flagged as an ad.
  - `test_relative_urls_end_and_map`:
    - expect `parse_media_playlist("…seg1.ts…", "https://h/a/b/index.m3u8").segments[0].url == "https://h/a/b/seg1.ts"`;
    - expect `ended` to be True when `#EXT-X-ENDLIST` is present and `has_map` to be True when `#EXT-X-MAP:URI="init.mp4"` is present.
  - `test_no_program_date_time`: every `start is None`.
  - `test_is_master`: True for a playlist with `#EXT-X-STREAM-INF`, False for a media playlist.
- [ ] **Step 2: Run them and see them fail.** `python -m pytest tests/test_live_hls.py -q`. They fail with `ModuleNotFoundError: baitd.live`.
- [ ] **Step 3: Implement `hls.py`** as specified above. Use only the standard library: `re`, `urllib.parse`, `datetime`.
- [ ] **Step 4: Run them and see them pass.** Same command: PASS.
- [ ] **Step 5: Commit** with the message `feat(live): HLS media playlist parser with ad and time detection`.

---

### Task 2: Ring buffer of segments, and low-priority FFmpeg

**Files:**
- Create: `src/baitd/live/buffer.py`
- Modify: `src/baitd/ffmpeg.py` (`run`)
- Test: `tests/test_live_buffer.py`, `tests/test_ffmpeg.py`

**Interfaces:**
- Consumes: `hls.Segment` from Task 1.
- Produces:
  - Constants `BUFFER_S = 300.0`, `REVIEW_PAD_S = 20.0` and `GAP_TOLERANCE_S = 0.5`.
  - `class NoFootage(Exception)`.
  - `buffer.Cut(path: Path, start: float, end: float, duration: float, gap: bool)`. `start` and `end` are the mark's bounds **in file seconds**. `gap` is True when a segment is missing inside the mark (an ad or a lost segment).
  - `class RingBuffer(folder: Path, keep_s: float = BUFFER_S)` with these methods:
    - `add(seq: int, start: float, duration: float, data: bytes) -> None`, which writes `folder/<seq:010d>.ts`;
    - `head() -> float | None` (the end of the newest segment) and `tail() -> float | None` (the start of the oldest);
    - `seconds() -> float`, the sum of the durations;
    - `pin(t: float | None) -> None`, after which segments that end after `t` are never pruned;
    - `cut(t0: float, t1: float, out: Path, pad: float = REVIEW_PAD_S) -> Cut`;
    - `clear() -> None`.
  - `ffmpeg.run(..., low_priority: bool = False)`:
    - on Windows it adds `BELOW_NORMAL_PRIORITY_CLASS` (`0x4000`) to `creationflags`;
    - on POSIX it passes `preexec_fn=lambda: os.nice(10)`.

**Rules:**
- **Pruning:** after each `add`, drop the oldest segments (both the file and the index entry) while `head - oldest.end > keep_s`. Never drop a segment that ends after the pin.
- **Cut:**
  1. Select the segments with `end > t0 - pad` and `start < t1 + pad`. If there are none, raise `NoFootage`.
  2. Write an FFmpeg concat list.
  3. Run `-f concat -safe 0 -i list.txt -c copy -movflags +faststart out` with `low_priority=True`.
  4. The file time of an epoch time `t` is the sum of the durations of the selected segments before the segment containing `t`, plus `t - seg.start`. Clamp it inside the file.
  5. `gap` is True when two consecutive selected segments with `start < t1` and `end > t0` are more than `GAP_TOLERANCE_S` apart (`next.start - (prev.start + prev.duration)`).

- [ ] **Step 1: Write the failing tests.**
  - **Fixture `segments(tmp_path)`:** use `ffmpeg.ffmpeg_exe()` to make an 8 s `testsrc=size=320x180:rate=30` video with a `sine` tone, written as MPEG-TS with `-f segment -segment_time 2 -segment_format mpegts`. That gives 4 files of 2 s.
  - `test_cut_inside_the_buffer`:
    - add the 4 segments at starts `1000, 1002, 1004, 1006`;
    - call `cut(1003, 1006, out)`;
    - expect `ffmpeg.probe(out)["duration"] ≈ 8 ± 0.2`, `cut.start ≈ 3.0 ± 0.1`, `cut.end ≈ 6.0 ± 0.1` and `not cut.gap`.
  - `test_cut_across_a_gap`:
    - skip the segment that starts at 1004;
    - call `cut(1001, 1007, out)`;
    - expect `cut.gap`, a probed duration `≈ 6 ± 0.2` and `cut.end ≈ 5.0 ± 0.1` (1007 falls in the last segment, which starts at file second 4).
  - `test_prune_and_pin`:
    - with `keep_s=4`, add all 4 segments;
    - expect `seconds() <= 6`, the oldest `.ts` file to be gone from disk and `tail() == 1002`;
    - then `pin(1001)` before adding a fifth segment at 1008: the segment that started at 1002 is still there.
  - `test_cut_outside_raises`: `cut(2000, 2010, out)` raises `NoFootage`.
  - `tests/test_ffmpeg.py::test_low_priority_flag`: monkeypatch `subprocess.Popen` to capture its kwargs; on Windows `creationflags & 0x4000`, on POSIX `preexec_fn` is set.
- [ ] **Step 2: Run them and see them fail.** `python -m pytest tests/test_live_buffer.py tests/test_ffmpeg.py -q`. They fail on the missing module and the missing parameter.
- [ ] **Step 3: Implement `RingBuffer` and `low_priority`** as specified above. Keep the index as a list sorted by start, guarded by a `threading.Lock`: the capture thread adds while the seal worker cuts.
- [ ] **Step 4: Run them and see them pass.** Same command: PASS.
- [ ] **Step 5: Commit** with the message `feat(live): 5-minute segment ring buffer with gap-aware cuts`.

---

### Task 3: Live moments stored as source + analysis

**Files:**
- Create: `src/baitd/live/store.py`
- Modify:
  - `src/baitd/clip/sources.py`: add `add_local_source`;
  - `src/baitd/clip/models.py`: add the `Moment` fields `badge` and `cam`, and `BADGES`;
  - `src/baitd/clip/pipeline.py`: `_moment_from_dict` and `sweep`.
- Test: `tests/test_live_store.py`, `tests/test_clip_pipeline.py`

**Interfaces:**
- Consumes: `buffer.Cut` (Task 2) and `channels.Channel` (Task 4). Until Task 4 lands, use a duck-typed object with `platform`, `handle`, `mine`, `framing`.
- Produces:
  - **Models:**
    - `clip.models.BADGES = ("kick", "twitch", "youtube")`;
    - `Moment.badge: str = ""` and `Moment.cam: list[int] | None = None`, added after `credit`;
    - `_moment_from_dict` accepts `badge` from `("", *BADGES)` and `cam` as `None` or a list of 4 non-negative ints; anything else raises `ValueError`.
  - **`sources.add_local_source(path: Path, title: str, channel: str, url: str) -> SourceInfo`:**
    - creates a new id folder, moves `path` there as `video.mp4`, and runs `_build_info` and `_write_meta`;
    - on any failure the folder is removed.
  - **`store.save_moment(cut: Cut, *, channel, title: str, reason: str, at: float, session_id: str, framing: str, kind: str = "interesante", classified: bool = False, description: str = "", hashtags: Sequence[str] = (), text: str = "") -> str`:**
    - returns the analysis id;
    - `reason` is one of `REASONS = ("manual", "chat", "laughs", "loud")`.
  - **`store.list_moments() -> list[dict]`:** newest first. Each entry has `analysis_id`, `source_id`, `title`, `reason`, `at` (ISO local), `kind`, `classified`, `platform`, `handle`, `mine`, `duration`, `gap` and `has_thumb`.
  - **`store.thumb_path(analysis_id: str) -> Path | None`.**
  - **`store.update_moment(analysis_id: str, **fields) -> None`:** the only fields accepted are `title`, `description`, `hashtags`, `kind`, `classified` and `transcript`.
  - **Sweep:** `clip_pipeline.sweep` never deletes an analysis whose JSON has `"origin": "live"`.

**Analysis JSON written by `save_moment`** (it must pass `_validate_analysis`):

```json
{"source_id": "<id>", "profile": ClipProfile().to_dict(), "created_at": "<iso>", "origin": "live",
 "transcript": {"language": "", "words": [], "duration": <cut.duration>},
 "moments": [{"start": cut.start, "end": cut.end, "kind": kind, "score": 0, "title": title,
              "description": description, "hashtags": [...], "text": text, "framing": framing,
              "credit": "" if channel.mine else "@" + handle_without_at, "badge": "" if channel.mine else channel.platform,
              "cam": null}],
 "live": {"platform": ..., "handle": ..., "mine": ..., "reason": reason, "at": "<iso local>",
          "session": session_id, "classified": classified, "gap": cut.gap}}
```

- The source's `channel` is `""`, so the old automatic "Fuente: @canal" never appears (R8). Its `title` is the `title` argument and its `url` is the channel's URL.
- The thumbnail is one frame at the moment's midpoint, written to `<source folder>/thumb.jpg` with `-vf scale=320:-2 -frames:v 1` and `low_priority=True`. A failed thumbnail is logged and ignored.

- [ ] **Step 1: Write the failing tests.**
  - `test_save_and_list`:
    - make a 12 s test MP4 (reuse the lavfi recipe from Task 2) and wrap it in `Cut(path, 3.0, 9.0, 12.0, False)`;
    - save it for a fake channel that is not the user's (`platform="kick", handle="westcol"`);
    - expect `list_moments()[0]` to have `reason == "manual"`, `platform == "kick"` and `has_thumb is True`;
    - expect `load_analysis(id)["moments"][0]` to have `credit == "@westcol"` and `badge == "kick"`;
    - expect `load_source(...).channel == ""`.
  - `test_own_channel_has_no_credit`: with `mine=True`, `credit == ""` and `badge == ""`.
  - `test_update_moment_rejects_other_fields`: `update_moment(id, start=1)` raises `ValueError`; `update_moment(id, title="X")` persists.
  - `test_sweep_keeps_live_analyses`:
    - run `clip_pipeline.sweep(set(), set(), keep_sources=False)`;
    - the live analysis and its source survive, while a plain analysis does not.
  - `tests/test_clip_pipeline.py::test_moment_badge_and_cam_validation`:
    - `badge="tiktok"` and `cam=[1, 2, 3]` each raise `ValueError`;
    - `cam=[0, 0, 320, 180]` is accepted.
- [ ] **Step 2: Run them and see them fail.** `python -m pytest tests/test_live_store.py tests/test_clip_pipeline.py -q`.
- [ ] **Step 3: Implement it** as specified above. Write the JSON with `config._write_json`.
- [ ] **Step 4: Run them and see them pass.** `python -m pytest -m "not network" -q`: the whole suite passes, because the existing analysis tests must keep passing.
- [ ] **Step 5: Commit** with the message `feat(live): store live moments as a source plus analysis`.

---

### Task 4: Channels: parse, store, live status, stream URL

**Files:**
- Create: `src/baitd/live/channels.py`
- Test: `tests/test_live_channels.py`

**Interfaces:**
- Produces:
  - `PLATFORMS = ("kick", "twitch", "youtube")`, `DEFAULT_DELAY_S = {"kick": 8.0, "twitch": 6.0, "youtube": 15.0}` and `MAX_CHANNELS = 50`.
  - `@dataclass Channel(id: str, platform: str, handle: str, url: str, mine: bool = False, framing: str = "cara", delay_s: float | None = None)`, with `to_dict()` and `from_dict()`. `framing` is one of `("cara", "partido")`; `delay_s` is `None` or 0–60.
  - `parse_channel(text: str) -> tuple[str, str, str]`, returning `(platform, handle, canonical_url)`. It uses `sources.normalize_url` first. Otherwise it raises `UserError("Ese enlace no es de un canal de Kick, Twitch o YouTube", "Pega el enlace del canal, por ejemplo kick.com/tu_canal")`.
  - `load_channels() -> list[Channel]`, `save_channels(chs: list[Channel]) -> None` (stored in `data_dir()/"canales.json"`) and `add_channel(text: str, mine: bool) -> Channel`.
    - `add_channel` raises a `UserError` for a duplicate of the same platform and handle: "Ese canal ya está en la lista".
    - It raises a `UserError` past `MAX_CHANNELS`.
  - `@dataclass LiveStatus(live: bool, title: str = "", viewers: int | None = None, name: str = "", checked_at: float = 0.0)`.
  - `live_status(ch: Channel, *, http=requests, extract=None) -> LiveStatus`:
    - **Kick:** `GET https://kick.com/api/v2/channels/<slug>` with `{"Accept": "application/json", "User-Agent": "Mozilla/5.0"}` and a 15 s timeout. `livestream` null means offline. Otherwise `title=livestream.session_title`, `viewers=livestream.viewer_count` and `name=user.username`.
    - **Twitch and YouTube:** `extract(url)` (default: yt-dlp `extract_info(url, download=False)` with `js_runtime_opts()`). An error containing "not currently live", "is offline" or "UserNotLive", or an `is_live` that is not True, means offline. Otherwise `title=info["title"]`, `viewers=info.get("concurrent_view_count")` and `name=info.get("uploader")`.
    - Any other failure returns `LiveStatus(False)` and logs a warning, without the URL query.
  - `pick_format(info: dict, max_height: int) -> dict`:
    - it considers only formats whose protocol starts with `m3u8`, whose `vcodec != "none"` and `acodec != "none"`, and whose `height` is at most `max_height`;
    - among those, it picks the highest height, then the highest `tbr`;
    - if there are none, it raises `UserError("No se encontró el video del directo", "Prueba otra calidad en Ajustes o vuelve a intentarlo")`.
  - `stream_url(ch: Channel, max_height: int, *, extract=None) -> tuple[str, dict]`, returning `(media playlist url, info)`. It raises `Offline(Exception)` when the channel is not live.

**Canonical forms:**

| Input | Result |
|---|---|
| `kick.com/Westcol` | `("kick", "westcol", "https://kick.com/westcol")` |
| `https://www.twitch.tv/LofiGirl?x=1` | `("twitch", "lofigirl", "https://www.twitch.tv/lofigirl")` |
| `youtube.com/@LofiGirl` or `m.youtube.com/@LofiGirl/live` | `("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live")` |
| `youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx` | `("youtube", "UC…", "https://www.youtube.com/channel/UC…/live")` |

- Kick slugs match `[a-z0-9_-]{2,40}` after lowercasing.
- Twitch logins match `[a-z0-9_]{3,25}`.
- YouTube handles match `@[\w.-]{3,40}`.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_channel`: the table above, plus rejections for `kick.com/`, `tiktok.com/@x` and `twitch.tv/videos/123`.
  - `test_store_roundtrip_and_duplicates`: uses the `tmp_path` data-dir fixture from `tests/conftest.py`.
  - `test_kick_status`: a fake `http.get` returns the JSON with `livestream` null, then a live payload; check `live`, `title` and `viewers`.
  - `test_ytdlp_status_offline_and_live`: the fake `extract` raises `Exception("…: The channel is not currently live")`, then returns `{"is_live": True, "title": "t"}`.
  - `test_pick_format`: formats `160p`, `720p60` and `1080p60` (with Kick-style formats where `acodec` is absent), a DASH video-only format with `acodec: "none"`, and a `1440p` format. With `max_height=1080`, it picks `1080p60`.
  - Network tests (`@pytest.mark.network`):
    - `test_network_kick_live_url`: take the first slug from `featured-livestreams/es` and check that `stream_url` returns a URL whose playlist `parse_media_playlist` reads with at least 1 segment;
    - `test_network_twitch_lofigirl_status`.
- [ ] **Step 2: Run them and see them fail.** `python -m pytest tests/test_live_channels.py -m "not network" -q`.
- [ ] **Step 3: Implement `channels.py`** as specified above.
- [ ] **Step 4: Run them and see them pass.** Run the same command, then once with `-m network`. The network run needs a live Kick channel, which the featured list provides.
- [ ] **Step 5: Commit** with the message `feat(live): channel list, live status and stream URL for Kick, Twitch, YouTube`.

---

### Task 5: Capture thread

**Files:**
- Create: `src/baitd/live/capture.py`
- Test: `tests/test_live_capture.py`

**Interfaces:**
- Consumes: Tasks 1, 2 and 4.
- Produces:
  - Constants `FIRST_FETCH_S = 60.0`, `RESOLVE_BACKOFF_S = (5, 15, 30)` and `MIN_FREE_BYTES = 2 * 1024**3`.
  - `class Capture(threading.Thread)`, constructed as `__init__(channel, buffer: RingBuffer, max_height: int, on_event: Callable[[str, dict], None], *, resolve=channels.stream_url, http: requests.Session | None = None, clock=time.time, wait: Callable[[float], bool] | None = None)`.
    - `wait(s)` returns True when the capture has been stopped. The default is `self._stop.wait`.
  - It exposes:
    - `state: str`, one of `"starting"`, `"capturing"`, `"reconnecting"`, `"ended"` and `"error"`;
    - `title: str`, `info: dict` (the yt-dlp info: `id`, `uploader`, …) and `has_pdt: bool`;
    - `offset() -> float`, the median of the last 5 measurements of `server_time - local time at response`. It falls back to the HTTP `Date` header, then to 0.0;
    - `server_now() -> float`, which is `clock() + offset()`;
    - `latency() -> float | None`, which is `server_now() - buffer.head()`;
    - `on_segment: Callable[[Path, float, float], None] | None`, a hook for the detector (Task 13) taking `(file, start, duration)`;
    - `stop(timeout: float = 10.0) -> None`.
  - Events sent to `on_event`: `"started"`, `"reconnecting"`, `"ended"`, `"disk_full"`, `"unsupported"` and `"error"`, each with a `{"text": str}` payload.

**Loop:**
1. Call `resolve(channel, max_height)`. On `Offline`, set state `ended` and send `ended`.
2. Poll the media playlist every `max(1.0, target_duration / 2)` s.
3. **First poll:** keep only the segments that end within `FIRST_FETCH_S` of the newest segment's end.
4. **Each new segment** that is not an ad gets a start time, is downloaded with a 15 s timeout, and goes into `buffer.add`. Then `on_segment` is called.
   - The start is `seg.start` when the playlist has program-date-times.
   - Otherwise the start is synthetic: the first segment gets `server_now() - (channel.delay_s or DEFAULT_DELAY_S[platform]) - seg.duration`, and later segments chain by sequence (R5).
5. **Already-seen sequences** are skipped. A drop in the sequence number means the stream restarted: reset the seen set and keep the buffer.
6. **An HTTP error or a timeout** on the playlist or a segment:
   1. set state `reconnecting` and resolve again, with `RESOLVE_BACKOFF_S` between attempts;
   2. after the third failure, set state `error` and send `"error"`.
   - An `Offline` from the resolve ends the capture with state `ended`.
7. **`EXT-X-ENDLIST`** ends the capture with state `ended`.
8. **`has_map`** stops the capture with state `error` and the event `"unsupported"` (R10).
9. **Free disk space** (`shutil.disk_usage(buffer folder)`) below `MIN_FREE_BYTES` stops the capture with the event `"disk_full"`.

**Logging:** log playlist and segment URLs only as `scheme://host/…`.

- [ ] **Step 1: Write the failing tests.** Use a `FakeHttp` whose `get(url)` returns queued playlist texts and segment bytes. Each response has a `.headers` dict and a `.status_code`. `clock` and `wait` are fakes that advance time with no real sleeping.
  - `test_downloads_each_segment_once`: three polls with overlapping windows add each `seq` exactly once, in order.
  - `test_first_poll_takes_last_60_s`: a YouTube-like playlist with 720 segments of 5 s leads to 12 downloads on the first poll.
  - `test_ads_never_enter_the_buffer`: segments flagged as ads are never requested and never added.
  - `test_prefetch_ignored`.
  - `test_offset_from_server_time`:
    - the playlist's `X-SERVER-TIME` is 4.5 s ahead of the fake clock;
    - expect `offset() ≈ 4.5` and `server_now() == clock() + 4.5`.
  - `test_reconnect_then_end`: a 403 on the playlist calls `resolve` again; when the second resolve raises `Offline`, the state becomes `"ended"` and the event `ended` is sent once.
  - `test_endlist_ends` and `test_map_is_unsupported`.
  - `test_synthetic_times_without_pdt`: uses `DEFAULT_DELAY_S["kick"]`.
- [ ] **Step 2: Run them and see them fail.** `python -m pytest tests/test_live_capture.py -q`.
- [ ] **Step 3: Implement `Capture`** as specified above. It is a daemon thread. `run()` catches every exception, sets state `error` and logs it.
- [ ] **Step 4: Run them and see them pass.** Same command: PASS.
- [ ] **Step 5: Commit** with the message `feat(live): capture thread with reconnection, ad skipping and server clock`.

---

### Task 6: Mark tracker (pure logic)

**Files:**
- Create: `src/baitd/live/marks.py`
- Test: `tests/test_live_marks.py`

**Interfaces:**
- Produces:
  - `EXTEND_S = 15.0` and `MAX_MARK_S = 180.0`.
  - `@dataclass Mark(start: float, end: float, presses: int = 1)`.
  - `class MarkTracker(before_s: float = 45.0, after_s: float = 15.0)` with these methods:
    - `press(t: float) -> Mark`:
      - with no pending mark, it opens `Mark(t - before_s, t + after_s)`;
      - if a mark is pending and `t <= pending.end`, it sets `pending.end = min(pending.end + EXTEND_S, pending.start + MAX_MARK_S)` and increments `presses`;
      - it returns the pending mark.
    - `pending() -> Mark | None`.
    - `ready(head: float | None) -> Mark | None`: if `head is not None` and `head >= pending.end`, it removes the pending mark and returns it.
    - `flush() -> Mark | None`: removes the pending mark and returns it.
    - `clamp_start(tail: float | None)`: called by the manager, it moves `pending.start` up to `tail` when the buffer is shorter than `before_s`, for example just after the capture started.

- [ ] **Step 1: Write the failing tests.**
  - `test_press_opens_window`: `press(100)` gives `(55, 115)`.
  - `test_presses_extend_and_cap`:
    - `press(100)` then `press(110)` gives `end == 130` and `presses == 2`;
    - ten more presses at `t=111` cap the end at `55 + 180 = 235`.
  - `test_ready_and_new_mark`:
    - `ready(114)` returns None and `ready(115)` returns the mark;
    - after that, `press(200)` opens a new mark at `(155, 215)`.
  - `test_flush`.
  - `test_clamp_start`: with tail 80, a mark that would start at 55 starts at 80.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Commit** with the message `feat(live): hotkey mark windows with extension and 3-minute cap`.

---

### Task 7: LiveManager, server routes, settings and app lifecycle

**Files:**
- Create: `src/baitd/live/session.py`
- Modify:
  - `src/baitd/config.py`: `SETTINGS_DEFAULTS`;
  - `src/baitd/web/server.py`: `create_app`, `post_settings` and the new routes;
  - `src/baitd/__main__.py`: `_serve` and `_close_everything`.
- Test: `tests/test_live_session.py`, `tests/test_server_live.py`

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:
  - **Settings** in `SETTINGS_DEFAULTS`: `"live_hotkey": "F9"`, `"live_before_s": 45`, `"live_after_s": 15`, `"live_ai_per_hour": 6` and `"live_max_height": 1080`.
    - `post_settings` validates `live_before_s` as an int from 10 to 120, `live_after_s` as an int from 5 to 60, `live_ai_per_hour` as an int from 0 to 30, `live_max_height` as one of `(720, 1080)` and `live_hotkey` through `hotkey.parse_hotkey` (Task 8).
    - Until Task 8 lands, validate `live_hotkey` as a non-empty string of at most 40 characters.
  - **`class LiveManager(data_dir: Path, settings: Callable[[], dict], *, capture_factory=Capture, hotkey_factory=None, clock=time.time)`:**
    - `start(channel_id: str) -> None`. It raises `UserError` for:
      - an unknown channel: "Ese canal ya no está en la lista";
      - a capture already running: "Ya hay una captura en marcha", "Detenla antes de empezar otra".
      - It clears `data_dir/"directo"/"buffer"` first.
    - `stop() -> None`: flushes and seals the pending mark, stops the capture, clears the buffer.
    - `mark(origin: str = "button") -> dict`, which returns the event it sent:
      - **No capture:** the event `no_capture`, whose text is "No hay ninguna captura activa → Elige un canal y pulsa Capturar".
      - **During a capture:** `t` is `capture.server_now()` when the channel is the user's and `buffer.head()` otherwise (R5); `tracker.press(t)` and then `tracker.clamp_start(buffer.tail())` are called.
      - **Buffer pin:** the buffer is pinned at `pending.start - REVIEW_PAD_S`.
    - **Ticks:** a manager thread ticks every 0.5 s and calls `tracker.ready(buffer.head())`. A ready mark goes to a seal worker thread, which cuts and saves it.
      - The cut uses `pad=REVIEW_PAD_S`.
      - The save uses `save_moment(..., reason="manual", framing=channel.framing, title=f"{display name} · {HH:MM}")`.
      - If the cut is shorter than `MIN_CLIP_S` (10), the event is `too_short`. On `NoFootage` the event is `no_footage`.
    - **Capture ended:** when the capture state becomes `ended` or `error`, the pending mark is flushed and sealed.
    - `status(since: int = 0) -> dict` returns:
      - `state` (`idle`, or the capture's state), `channel_id`, `title`, `latency_s` and `buffered_s`;
      - `pending`: `{"remaining_s", "length_s"}` or `None`, where `remaining_s = max(0, pending.end - (server_now - latency))`;
      - `events`: the events with an id greater than `since`, kept in a ring of the last 30, each `{id, kind, text}`;
      - `has_pdt` (from the capture; the UI shows the delay field only when it is false);
      - `ai_left`, which is `None` until Task 13 wires the `HourlyLimit`.
    - `close() -> None` is called on app exit: stop, then unregister the hotkey.
    - `channels_changed() -> None` registers or unregisters the hotkey under R4. It is a no-op until Task 8.
  - **Routes** in `create_app(queue, shutdown=None, focus=None, live: LiveManager | None = None)`. When `live` is None, the app builds a `LiveManager(config.data_dir(), config.load_settings)`; no thread starts until it is used.
    - `GET /api/live/channels` returns the channels plus their cached `LiveStatus` fields.
    - **Status refresh:** a background thread refreshes the statuses every 180 s while any client asked in the last 10 minutes. It refreshes on `POST /api/live/channels/refresh`, with at most 4 workers.
    - `POST /api/live/channels {url, mine}`, `PATCH /api/live/channels/<id> {mine?, framing?, delay_s?}` and `DELETE /api/live/channels/<id>`. The DELETE returns 409 while that channel is being captured.
    - `POST /api/live/capture {channel_id}` and `DELETE /api/live/capture`.
    - `POST /api/live/mark`, which returns the event.
    - `GET /api/live/status?since=N`.
    - `GET /api/live/moments`, which returns `store.list_moments()`.
    - `GET /api/live/moments/<analysis_id>/thumb`, which returns the JPEG, or 404.
    - Every mutating route is JSON, as the existing guard already enforces.
  - **App lifecycle in `__main__`:**
    - `_serve` builds the `LiveManager` and passes it to `create_app`.
    - `_close_everything` calls `live.close()` before the queue stops.
    - At startup, `data_dir/"directo"/"buffer"` is deleted.

- [ ] **Step 1: Write the failing tests.** They use a `FakeCapture` with a settable `state`, `server_now()`, a real `RingBuffer` fed with the Task 2 segments, and a fake clock.
  - `test_manual_mark_uses_server_time`:
    - the user's channel, `FakeCapture.server_now() == 1006.0` while the buffer head is 1004.0;
    - press;
    - expect the pending mark to start at `961.0`, which is `1006 - 45`, not at `959.0`.
  - `test_other_channel_mark_uses_head`.
  - `test_mark_without_capture`: the event kind is `no_capture`.
  - `test_ready_mark_is_saved`: advance the head past the mark's end; within 2 ticks, `list_moments()` has 1 entry with `reason == "manual"`.
  - `test_stream_end_seals_pending_mark`:
    - set the capture state to `ended` while a mark is pending with 12 s of footage, and the moment is saved;
    - with 5 s of footage, the event is `too_short` and nothing is saved.
  - `test_close_seals_and_clears_buffer`: after `close()`, the moment is saved and the buffer folder is empty.
  - `test_second_start_refused`.
  - `tests/test_server_live.py`:
    - channel CRUD through the routes;
    - a 409 on deleting the channel being captured;
    - `POST /api/live/mark` with no capture returns `kind == "no_capture"`;
    - `GET /api/live/moments` returns the saved moment;
    - the thumb route answers 404 for a bad id;
    - `POST /api/settings` with `live_before_s=500` returns 400.
- [ ] **Step 2: Run them and see them fail.** `python -m pytest tests/test_live_session.py tests/test_server_live.py -q`.
- [ ] **Step 3: Implement the manager, the routes, the settings and the lifecycle** as specified above.
- [ ] **Step 4: Run them and see them pass.** Then run the whole suite: `python -m pytest -m "not network" -q`.
- [ ] **Step 5: Commit** with the message `feat(live): live manager, routes and settings`.

---

### Task 8: Global hotkey

**Files:**
- Create: `src/baitd/live/hotkey.py`
- Modify:
  - `src/baitd/live/session.py`: register the hotkey under R4, and `reload_hotkey()`;
  - `src/baitd/web/server.py`: validate the setting and call `live.reload_hotkey()` after saving;
  - `pyproject.toml`: the `pynput` dependency.
- Test: `tests/test_live_hotkey.py`

**Interfaces:**
- Produces:
  - `class HotkeyError(UserError)`.
  - `parse_hotkey(spec: str) -> tuple[frozenset[str], str]`:
    - modifiers come from `{"ctrl", "alt", "shift", "win"}`;
    - keys are `f1`–`f24`, `a`–`z`, `0`–`9`, `pause`, `insert`, `home`, `end`, `pageup` and `pagedown`;
    - the input is case-insensitive and joined by `+`;
    - a bad spec raises `HotkeyError("Esa tecla no se puede usar", "Elige una tecla de función (F1–F24) o una combinación con Ctrl, Alt o Mayús")`.
  - `format_hotkey(mods, key) -> str`, the canonical form, for example `"Ctrl+Shift+F9"`.
  - `class HotkeyListener(spec: str, on_press: Callable[[], None])` with `start() -> None` and `stop() -> None`.
    - `start()` raises `HotkeyError("La tecla {spec} ya la usa otro programa", "Elige otra en Ajustes")` when the system refuses it.
    - **Windows:**
      - a daemon thread calls `user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT(0x4000), vk)`, then runs a `GetMessageW` loop;
      - `WM_HOTKEY` (`0x0312`) calls `on_press`;
      - `stop()` posts `WM_QUIT` to that thread with `PostThreadMessageW` and calls `UnregisterHotKey` from the thread;
      - modifier values are `MOD_ALT=1`, `MOD_CONTROL=2`, `MOD_SHIFT=4` and `MOD_WIN=8`, and virtual keys are `VK_F1=0x70` and up.
    - **macOS and Linux:** use `pynput.keyboard.GlobalHotKeys({"<f9>": on_press})` when pynput imports.
      - If pynput fails to import or raises (Wayland, or Accessibility refused), `start()` raises `HotkeyError("No se pudo activar la tecla rápida", "Usa el botón Marcar de la ventana")` and the window's button stays.
  - **Manager:** `LiveManager.reload_hotkey()` and R4 registration.
    - `on_press` calls `mark("hotkey")`.
    - A registration failure becomes the event `hotkey_busy`, carrying the `HotkeyError` text.

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_and_format`:
    - `parse_hotkey("ctrl+shift+f9")` round-trips to `"Ctrl+Shift+F9"`;
    - `"F9"` is valid;
    - `""`, `"ctrl"`, `"f25"` and `"ctrl+ñ"` raise `HotkeyError`.
  - `test_register_uses_norepeat` (Windows only, `skipif` elsewhere):
    - monkeypatch a fake `user32` object whose `RegisterHotKey` records its arguments and returns 1, and whose `GetMessageW` returns 0 at once;
    - expect `mods & 0x4000` and `vk == 0x78`.
  - `test_busy_key_raises`: the fake `RegisterHotKey` returns 0, so `start()` raises a `HotkeyError` whose text contains "ya la usa otro programa".
  - `test_manager_registers_only_with_channels`:
    - with a fake listener factory, no channels means `start` is never called;
    - adding one channel calls `start` once, and removing it calls `stop`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** Keep the platform branches small, and load `ctypes.windll.user32` lazily so imports work on every system.
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Verify on Windows.** Start Baitd from source, add a channel, start a capture of a live Kick channel, focus another program and press F9.
  - The status shows a pending mark counting down.
  - After about 15 s plus the latency, a moment appears in `GET /api/live/moments`.
- [ ] **Step 6: Commit** with the message `feat(live): global hotkey (Windows RegisterHotKey, pynput elsewhere)`.

---

### Task 9: Export of live moments, and AI title on demand

**Files:**
- Create: `src/baitd/live/ai.py`
- Modify:
  - `src/baitd/llm/_http.py`: add `QuotaExhausted`;
  - `src/baitd/clip/pipeline.py`: `run_clip_export`, plus a new `run_live_title`;
  - `src/baitd/jobqueue.py`: the runner, the kinds and the `analysis_id` for `live_title`;
  - `src/baitd/web/server.py`: `POST /api/live/moments/<id>/ai`.
- Test: `tests/test_live_ai.py`, `tests/test_clip_pipeline.py`, `tests/test_jobqueue.py`, `tests/test_server_live.py`

**Interfaces:**
- Consumes: Task 3 (the analysis format and `update_moment`).
- Produces:
  - **`QuotaExhausted`:** `llm._http.QuotaExhausted(UserError)` is raised instead of the plain `UserError` for an exhausted 429, with the same text.
  - **`ai.AI_KINDS`:** `{"humor": "gracioso", "polemica": "polemico", "nada": "nada"}`.
  - **`ai.build_prompt(lines: str, language: str, *, want_span: bool) -> str`:** it asks for JSON only.
    - The JSON is `{"tipo": "humor|polemica|nada", "inicio": "m:ss", "fin": "m:ss", "titulo": str, "descripcion": str, "hashtags": [str]}`.
    - The title has at most `moments.TITLE_WORDS` words, in the language of the transcript.
    - `inicio` and `fin` are asked for only when `want_span` is set.
    - `lines` is in the same `[m:ss] text` format as `moments._window_lines`.
  - **`ai.parse_reply(raw: str, w0: float, w1: float, *, want_span: bool) -> AiVerdict | None`:**
    - it returns `AiVerdict(kind: str, start: float | None, end: float | None, title: str, description: str, hashtags: list[str])`;
    - the times are parsed with `moments._parse_time`, made absolute and clamped into `[w0, w1]`;
    - a span shorter than `MIN_CLIP_S` falls back to the whole window;
    - the reply may come wrapped in code fences;
    - an unreadable reply returns None.
  - **`ai.HourlyLimit(per_hour: int, clock=time.time)`:** a sliding hour, with `take() -> bool`, `left() -> int` and `exhaust() -> None` (no more requests until the session ends).
  - **`clip_pipeline.run_live_title(item, settings, cancel, progress, providers=None, **kw) -> Path`:** its payload is `{"analysis_id"}`.
    1. **Step 1:** load the analysis and its source.
    2. **Step 2:** transcribe the source when the analysis has no words, and save the transcript into the analysis with `update_moment(transcript=...)`.
    3. **Step 3:** call `complete(build_prompt(..., want_span=False))` once.
    4. **Update:** set `title`, `description` and `hashtags`. Set `kind` unless it is `nada`, and set `classified=True`.
    - It **never** changes `start` or `end`.
    - It returns the analysis path.
  - **`clip_pipeline.run_clip_export`:**
    - when the analysis transcript has no words, it runs step 2 (transcribe, reusing the `_understand` transcription code, refactored into `_transcribe(src, opts, settings, cancel, steps, providers) -> Transcript`) and saves the transcript into the analysis;
    - when `analysis.get("origin") == "live"`, it snaps each moment with `moments.snap_to_sentences(m.start, m.end, words, MIN_CLIP_S, MAX_CLIP_S)` when that returns a span.
  - **Queue:** `CLIP_JOB_KINDS` gains `"live_title"`; `jobqueue._CLIP_RUNNERS["live_title"] = clip_pipeline.run_live_title`; `add_clip_job` records `analysis_id` for `live_title` as it does for `clip_export`.
  - **Route:** `POST /api/live/moments/<analysis_id>/ai {profile_id}` queues `live_title` and returns `{job_id}`. It returns 404 for an analysis that is not live.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_live_ai.py`:
    - `test_prompt_asks_json_and_span_only_when_wanted`;
    - `test_parse_reply`: a fenced JSON with `inicio "0:05"` and `fin "0:40"` in the window `[100, 160]` gives `start == 105` and `end == 140`; `tipo "humor"` gives `kind == "gracioso"`; garbage gives None;
    - `test_parse_reply_short_span_uses_window`;
    - `test_hourly_limit`: 6 takes succeed, the 7th fails, an hour later 1 succeeds, and after `exhaust()` everything fails.
  - `tests/test_clip_pipeline.py`:
    - `test_export_transcribes_live_moment_once`: a live analysis with no words and a fake `providers.transcribe`; one export calls `transcribe` once and the analysis now has words;
    - `test_export_snaps_live_bounds`: a word list whose sentence ends at 9.6 s, with the moment ending at 9.0, gives a rendered moment that ends at 9.6;
    - `test_live_title_keeps_bounds`: a fake `complete` returns a title; the title changes and start and end stay the same.
  - `tests/test_jobqueue.py::test_live_title_kind`.
  - `tests/test_server_live.py::test_ai_route_queues_job`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.** The `_transcribe` refactor must leave `run_clip_auto` and `run_clip_analyze` working exactly as before.
- [ ] **Step 4: Run them and see them pass.** Run the whole suite.
- [ ] **Step 5: Commit** with the message `feat(live): export live moments and ask the AI for a title on demand`.

---

### Task 10: Platform logo next to the credit

**Files:**
- Modify:
  - `src/baitd/captions.py`: `write_clip_ass` and the badge shapes;
  - `src/baitd/clip/render.py`: pass the badge;
  - `src/baitd/web/server.py`: `_export_moments` keeps the badge from the analysis.
- Test: `tests/test_captions.py`, `tests/test_clip_render.py`, `tests/test_server_clip.py`

**Interfaces:**
- Consumes: `Moment.badge` (Task 3).
- Produces:
  - **`captions.write_clip_ass(..., credit: str = "", credit_badge: str = "")`:**
    - with a badge from `BADGES` and a non-empty credit, it adds one Dialogue on the Credit layer that draws the logo as an ASS vector drawing (`{\p1}`), as tall as the credit text, at the credit's left edge;
    - the credit text moves right by the logo's width plus `0.3 ×` the credit size.
  - **Logo shapes and colours**, kept simple in the spirit of spec §6.7, defined in a 100-unit box and scaled with `\fscx` and `\fscy`:

    | Badge | Shape |
    |---|---|
    | `kick` | a `#53FC18` rounded square with a black "K" made of polygons |
    | `twitch` | a `#9146FF` speech-bubble outline with two white bars |
    | `youtube` | a `#FF0000` rounded rectangle with a white play triangle |

    ASS colours are written `&HBBGGRR&`; for Kick that is `&H18FC53&`.
  - **`render_clip`:** it passes `credit_badge=m.badge if credit else ""`.
  - **`_export_moments`:** it copies `badge` from the analysis moment (`base`), never from the browser payload.

- [ ] **Step 1: Write the failing tests.**
  - `test_credit_badge_drawn`:
    - the ASS output contains `\p1` and `&H18FC53&`;
    - the `\pos` x of the credit text is greater than `_SAFE_MARGIN + credit size`;
    - with no badge, the output matches today's byte for byte (compare against a run without the parameter).
  - `test_render_passes_badge`: a small render with `Moment(badge="twitch", credit="@x")` writes an ASS file containing `&HFF4691&`. Inspect the work dir before cleanup, through a monkeypatched `captions.write_clip_ass` spy.
  - `test_export_ignores_browser_badge`: posting `badge: "youtube"` for a Kick analysis still queues `badge == "kick"`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Look at it.**
  1. Render one real 1080×1920 test clip with each badge, using the selftest media.
  2. Grab one frame each.
  3. Check that the logo is readable, aligned with the text and inside the safe zone.
  4. Attach the three frames to the task report.
- [ ] **Step 6: Commit** with the message `feat(clip): platform logo next to the credit on others' live clips`.

---

### Task 11: "Partido" layout (cam on top, game below)

**Files:**
- Modify:
  - `src/baitd/clip/models.py`: `FRAMINGS`;
  - `src/baitd/clip/framing.py`: `CropPlan`, `find_cam`, `plan_crop` and `crop_filter`;
  - `src/baitd/clip/pipeline.py`: `_render_all` passes `cam` and detects faces for partido;
  - `src/baitd/clip/render.py`: the partido plan needs no key remap;
  - `src/baitd/web/server.py`: `_export_moments` validates `cam`.
- Test: `tests/test_clip_framing.py`, `tests/test_clip_render.py`, `tests/test_server_clip.py`

**Interfaces:**
- Produces:
  - **Layout values:** `FRAMINGS = ("cara", "centro", "completo", "partido")`, `TOP_SHARE = 0.4` (the cam band is `_even(out_h * 0.4)`, which is 768 for 1920), `CAM_MIN_SHARE = 0.6`, `CORNER = 0.35`, `CAM_MAX_AREA = 0.06` and `CAM_FACE_SCALE = 3.0`.
  - **`CropPlan`** gains `cam: tuple[int, int, int, int] | None = None` and `game: tuple[int, int, int, int] | None = None`. Both are `(x, y, w, h)` in source pixels.
  - **`find_cam(samples, src_w: int, src_h: int, out_w: int, top_h: int) -> tuple[int, int, int, int] | None`:**
    1. Take the main track (`_main_track`). Return None unless it appears in at least `CAM_MIN_SHARE` of the samples.
    2. Its median centre must lie in a corner: x below `CORNER·w` or above `(1-CORNER)·w`, **and** y likewise. The median face area must be below `CAM_MAX_AREA·w·h`. Otherwise return None.
    3. The box is centred on the median face centre, with height `CAM_FACE_SCALE ×` the median face height and width `height × out_w / top_h`.
    4. Clamp the box inside the frame and round it to even numbers.
  - **`plan_crop(..., cam: list[int] | None = None)` with `mode == "partido"`:**
    - a source that is not landscape (`src.width <= src.height`) goes down the `cara` path;
    - otherwise the cam box is `cam`, made even and clamped, when the user gave one, else `find_cam(faces or detect_faces(...))`;
    - with no box, it takes the `cara` path;
    - the game box has height `src.height`, width `min(src.width, _even(src.height * out_w / (out_h - top_h)))` and x centred in the frame (R7).
  - **`crop_filter`** for partido returns exactly:

    ```
    f"split[pa][pb];[pa]crop={cw}:{ch}:{cx}:{cy},scale={out_w}:{top_h}:flags=lanczos[pt];"
    f"[pb]crop={gw}:{gh}:{gx}:{gy},scale={out_w}:{out_h - top_h}:flags=lanczos[pg];[pt][pg]vstack"
    ```

  - **`_render_all`:**
    - it also runs `providers.detect_faces` when the mode is `partido`;
    - it passes `cam=m.cam`;
    - `_talk_and_voices` is not needed for partido.
  - **`_export_moments`:** `cam` is `None`, or 4 ints with `w, h >= 16` and `x + w <= src.width`, `y + h <= src.height`. Anything else raises `_bad_times`-style `UserError("El recuadro de la cámara no es válido", "Vuelve a marcar la cámara en la tarjeta")`.

- [ ] **Step 1: Write the failing tests.**
  - `test_find_cam_bottom_right`:
    - samples on a 1920×1080 source with one face `(1650, 850, 90, 90)` in 80% of the samples;
    - expect a box whose centre is near `(1695, 895)`, a width/height ratio of `1080/768 ± 1%`, and a box inside the frame.
  - `test_find_cam_centre_face_is_none`, `test_find_cam_rare_face_is_none` and `test_find_cam_big_face_is_none`.
  - `test_plan_partido_uses_user_cam`.
  - `test_plan_partido_without_cam_falls_back_to_cara`.
  - `test_partido_filter_string`: exact string equality with the template above for a known plan.
  - `test_render_partido_dimensions`: render a 4 s 1280×720 test source with `framing="partido"` and `cam=[1000, 500, 200, 142]`; `probe(out)` is 1080×1920.
  - `test_export_rejects_bad_cam`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass.** Run the whole suite. The existing framing tests must still pass.
- [ ] **Step 5: Commit** with the message `feat(clip): partido layout with automatic corner-cam detection`.

---

### Task 12: Chat readers (Twitch, Kick, YouTube)

**Files:**
- Create: `src/baitd/live/chat.py`
- Modify: `pyproject.toml` (`aiohttp>=3.9,<4`)
- Test: `tests/test_live_chat.py`

**Interfaces:**
- Produces:
  - **Pure parsers:**
    - `twitch_privmsgs(raw: str) -> tuple[list[str], bool]`, returning the message texts and whether a `PING` must be answered;
    - `kick_text(frame: dict) -> str | None`;
    - `youtube_parse(data: dict) -> tuple[list[str], str | None, int]`, returning the texts, the next continuation and the poll delay in ms.
  - **`class ChatReader(threading.Thread)`:** constructed as `(channel: Channel, on_message: Callable[[float, str], None], on_event, *, video_id: str = "")`, with `stop()`. It runs its own asyncio loop with aiohttp.
    - **Twitch:**
      - connect to `wss://irc-ws.chat.twitch.tv:443`;
      - send `CAP REQ :twitch.tv/tags twitch.tv/commands`, `PASS SCHMOOPIIE`, `NICK justinfan<5 random digits>` and `JOIN #<login>`;
      - answer `PING :tmi.twitch.tv` with `PONG :tmi.twitch.tv`.
    - **Kick:**
      - follow exactly the "Kick chat" sequence in the facts above;
      - `chatroom_id` comes from `kick.com/api/v2/channels/<slug>` → `chatroom.id`;
      - reconnect with a new token when the socket closes, because the token lasts about 45 min;
      - never log the token.
    - **YouTube:**
      - `GET https://www.youtube.com/live_chat?is_popout=1&v=<video_id>`, reading `INNERTUBE_CLIENT_VERSION` and the first continuation from the page;
      - then poll `POST https://www.youtube.com/youtubei/v1/live_chat/get_live_chat` with `{"context": {"client": {"clientName": "WEB", "clientVersion": V}}, "continuation": C}`, waiting the delay the reply gives;
      - the messages are `actions[].addChatItemAction.item.liveChatTextMessageRenderer.message.runs[]`, using `text` or `emoji.shortcuts[0]`;
      - yt-dlp's `yt_dlp/downloader/youtube_live_chat.py` is the reference for these structures.
    - **After 5 failed connects in a row**, send the event `chat_off`. Its text is "No se pudo leer el chat de {plataforma} → Baitd sigue solo con el audio". The reader then stops. This is spec §7's fallback.
  - **`make_reader(channel, on_message, on_event, video_id) -> ChatReader`.**

- [ ] **Step 1: Write the failing tests.** Each parser test uses a recorded frame.
  - `test_twitch_privmsgs`:
    - the input is `"@badge-info=;color=#FF0000 :u!u@u.tmi.twitch.tv PRIVMSG #chan :jajaja KEKW\r\nPING :tmi.twitch.tv\r\n"`;
    - expect `(["jajaja KEKW"], True)`.
  - `test_kick_text`: the push frame captured on 2026-11-15 (`content "es verdad eso asjsa"`) gives that text; a non-chat event gives None.
  - `test_youtube_parse`: a minimal JSON with one text message, one emoji message, `invalidationContinuationData.continuation "C2"` and `timeoutMs 5000`.
  - `test_reader_gives_up_after_5_failures`: the connect function is injected and always raises; exactly one `chat_off` event is sent.
  - Network tests (`@pytest.mark.network`): `test_network_kick_chat` takes a live channel from the featured list and expects at least 1 message within 60 s; `test_network_twitch_chat` uses a big live channel and the same expectation.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass.** Run once with `-m network`. If YouTube's chat cannot be read, keep the audio-only fallback and record it for the closing notes (Task 15).
- [ ] **Step 5: Commit** with the message `feat(live): anonymous chat readers for Twitch, Kick and YouTube`.

---

### Task 13: Automatic detection on other channels

**Files:**
- Create: `src/baitd/live/signals.py`, `src/baitd/live/detector.py`
- Modify: `src/baitd/live/session.py`: run the detector and the chat reader for channels that are not the user's; send `ai_left` in the status.
- Test: `tests/test_live_signals.py`, `tests/test_live_detector.py`

**Interfaces:**
- Consumes:
  - `Capture.on_segment` (Task 5), `ChatReader` (Task 12), `ai` (Task 9) and `store.save_moment` (Task 3);
  - `transcribe.transcribe`, called through an injectable function;
  - `llm.complete`.
- Produces:
  - **Starting thresholds**, to be calibrated in Step 5:

    | Constant | Value | Meaning |
    |---|---|---|
    | `BIN_S` | 10.0 | width of a chat bin |
    | `BASELINE_S` | 300.0 | history the baseline uses |
    | `MIN_HISTORY_S` | 60.0 | history needed before any spike |
    | `CHAT_RATE_X` | 3.0 | spike: this many times the baseline rate |
    | `CHAT_MIN_MSGS` | 8 | spike: at least this many messages in the bin |
    | `LAUGH_SHARE` | 0.35 | laughter spike: share of laughing messages |
    | `LAUGH_MIN` | 5 | laughter spike: at least this many laughing messages |
    | `LOUD_LU` | 8.0 | audio spike: LU above the median |
    | `COOLDOWN_S` | 45.0 | quiet time after a spike |

  - **`LAUGH`:** a compiled, case-insensitive regex that matches:
    - Spanish: `jaja…`, `jsjs…`, `xd`, `JAJA`;
    - English: `lol`, `lmao`, `haha…`;
    - Portuguese: `kkkk`;
    - emotes: `KEKW`, `LUL`, `OMEGALUL`, `ICANT`;
    - emoji: `😂`, `🤣`, `💀`.
  - `@dataclass Spike(t: float, reason: str, strength: float)`, where `reason` is one of `"chat"`, `"laughs"` or `"loud"`.
  - **`ChatSignal`:** `add(t: float, text: str)` and `check(now: float) -> Spike | None`.
    - It compares the newest full bin with the median bin of the last `BASELINE_S`.
    - **Laughter takes priority** over a plain rate spike.
    - It stays quiet for the first `MIN_HISTORY_S` and for `COOLDOWN_S` after a spike.
  - **`AudioSignal`:** `add(t: float, lufs: float)` and `check() -> Spike | None`, using the same history and cooldown rules against the median.
  - **`segment_loudness(path: Path) -> float | None`:** the maximum momentary loudness, from FFmpeg `-af ebur128` with `-vn` and `low_priority=True`.
  - **`class Detector`:** constructed as `(buffer, channel, settings: Callable[[], dict], profile: Profile, *, save, transcribe=None, complete=None, limit: HourlyLimit, clock)`.
    - The manager passes `profile = config.list_profiles()[0]`: the server cannot know which profile the window has selected. This is a ruling for the closing notes.
    - `spike(s: Spike)` opens a candidate `[s.t - CANDIDATE_BEFORE_S, s.t + CANDIDATE_AFTER_S]`, or extends an overlapping one (overlapping candidates merge).
    - `tick(head)` sends the candidates whose end is at or before `head` to one below-normal-priority worker thread, which handles them one at a time:
      1. Cut the window to a temp file. Extract the audio and transcribe it with the profile's clipping transcriber, model and language, using `cpu_threads=2` for the local model.
      2. With no words, save the moment unclassified with the spike's reason.
      3. If `limit.take()` succeeds:
         - call `complete(ai.build_prompt(..., want_span=True))`;
         - a `nada` verdict drops the candidate;
         - otherwise save the AI's span (re-cut it from the buffer with the review padding), title, kind and hashtags, with `classified=True`;
         - a `QuotaExhausted` calls `limit.exhaust()` and saves the moment unclassified.
      4. If there is no quota, save the moment unclassified.
    - `stop()`.
  - **Manager:** for a channel that is not the user's, the manager:
    - wires `capture.on_segment` to queue `segment_loudness` for the audio signal;
    - feeds the chat reader into `ChatSignal`;
    - checks both signals every tick and passes any spike to `detector.spike`;
    - nothing heavy runs on the capture thread itself.
  - **Model threads:** `transcribe.local_words` and `_load_model` gain an optional `cpu_threads: int | None = None`, passed to `WhisperModel`; the cache is keyed by `(model, cpu_threads)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_laugh_regex`: matches `jajaja`, `JSJSJS`, `kekw`, `😂`, `lmaooo`; does not match `javier` or `hola`.
  - `test_chat_spike_and_cooldown`:
    - 1 message per bin for 120 s, then 12 in one bin, gives a `chat` spike;
    - the next bin with 12 messages gives none (cooldown).
  - `test_laughs_win`: 10 messages, 7 of them laughing, give `laughs`.
  - `test_no_spike_before_history`.
  - `test_audio_spike`: a median of −30, then −20, gives a `loud` spike.
  - `test_detector_merges_and_classifies`:
    - two spikes 10 s apart give one candidate;
    - the fake `complete` returns humor with `inicio 0:10` and `fin 0:45`;
    - `save` is called once with `kind="gracioso"`, `classified=True` and `reason="chat"`.
  - `test_detector_drops_nada`.
  - `test_detector_quota_saves_unclassified`: the fake `complete` raises `QuotaExhausted`; the moment is saved with `classified=False`, and later candidates make no AI call.
  - `test_detector_respects_hourly_limit`: with `per_hour=1`, 3 candidates make 1 AI call and 2 unclassified saves.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run them and see them pass.** Run the whole suite.
- [ ] **Step 5: Calibrate on real streams** (network, about 40 minutes):
  1. Write `tools/live_calibrate.py`. It is kept in the repo and is not packaged. It runs `Capture` plus `ChatReader` plus both signals on one channel for N minutes, with no AI. It logs per-bin message counts, laughter shares, segment loudness and each spike.
  2. Run it 10 minutes each on 2 live Kick channels taken from the featured list and 2 live Twitch channels: one big chat and one small chat per platform.
  3. Tune the constants so that a busy chat gives about 2–8 spikes per 10 minutes and a quiet chat gives 0–2.
  4. Write the final values and the measured counts into `docs/decisiones-0.5.0.md`.
- [ ] **Step 6: Commit** with the message `feat(live): automatic moment detection from chat and audio with AI classification`.

---

### Task 14: The "En directo" tab (UI)

**Files:**
- Modify: `src/baitd/web/templates/index.html`, `src/baitd/web/static/app.js`, `src/baitd/web/static/app.css`, `src/baitd/web/static/i18n/es.json`, `src/baitd/web/static/i18n/en.json`
- Test: `tests/test_server.py` (the existing i18n and copy tests cover it), plus manual checks in the app.

**Interfaces:**
- Consumes: the routes from Tasks 7, 9 and 11, and the existing `openReview`, `momentCard`, `onExport` and `refreshJobs`.
- Produces:
  - **Sub-tabs:** the Clipping panel gets two sub-tabs, `Video` (today's content, unchanged) and `En directo`, built with the same accessible tab pattern as `setTab` and `onTabKey`.
  - **The "En directo" panel**, in Baitd's current style. Its look is left to the implementer, within `app.css` tokens.
    1. **Channels:**
       - an add field for the URL, a "Es mío" checkbox and an "Añadir" button;
       - a list, one row per channel, built from a new `<template id="tpl-channel">`;
       - each row shows the platform logo (small inline SVG), the handle, a live dot, the title and viewer count when live, an "Es mío" toggle, a Formato select (Cara/Partido), a delay input (shown only when `has_pdt` is false after a capture), a "Capturar" button and "Quitar".
       - The list refreshes when the tab opens and every 60 s while the tab is visible.
    2. **Capture box** (shown while a capture is active):
       - it shows the channel, the state, the latency (`Retraso: 6 s`), the buffered seconds (`Guardado: 4:12 de 5:00`) and the AI requests left this hour;
       - it has a large "Marcar momento" button and a "Detener" button;
       - while a mark is pending it shows `Guardando lo que sigue… 12 s · pulsa otra vez para alargar` (spec §6.3);
       - it polls `GET /api/live/status?since=` every 1 s while capturing and every 10 s otherwise;
       - the events become short notices in the box, for example `no_capture`, `hotkey_busy`, `too_short`, `ended`, `chat_off` and `disk_full`.
    3. **Moments list:**
       - one card per moment from `GET /api/live/moments`, built from `<template id="tpl-live-moment">`;
       - each card shows the thumbnail, the title, the reason (`live.reason.manual`: "Lo marcaste tú", `chat`: "El chat explotó", `laughs`: "Risas en el chat", `loud`: "Subida de volumen"), the local time, the duration, an "Hubo un anuncio" warning when `gap` is set, and an "Sin clasificar" tag when it is automatic and unclassified;
       - each card has a checkbox and the buttons "Revisar", "Título con IA" and "Quitar";
       - "Revisar" calls `openReview({analysis_id}, true)` and switches to the Video sub-tab, where the review card opens;
       - "Título con IA" calls `POST /api/live/moments/<id>/ai` with the current profile and refreshes the list when that job finishes;
       - "Quitar" calls `DELETE /api/analyses/<id>`, after a confirmation;
       - "Exportar seleccionados" posts `/api/analyses/<id>/export` once per checked moment, with that analysis's stored moment;
       - the list refreshes when the status reports a new `saved` event.
    4. **Partido cam editor** inside `momentCard`:
       - the framing select in `tpl-moment` gains a "Partido" option (`moment.framing.partido`);
       - when `framing === "partido"`, a rectangle overlay appears on the preview video, with a fixed aspect of `1080/768`;
       - dragging moves it and dragging the corner handle resizes it;
       - its value is stored in `m.cam` in source pixels and sent in the export payload.
  - **Ajustes:** add "Tecla rápida", which records the next key combination: press **Esc** to cancel, and the value is shown in canonical form. Also add "Segundos antes", "Segundos después", "Calidad del directo" (720p/1080p) and "Peticiones de IA por hora".
  - **`reviewSignature` and `onExport`** include `cam`.

- [ ] **Step 1: Write the failing tests.**
  - Add every new key under `live.*` to both JSON files. The existing `test_index_has_no_hardcoded_copy` and `test_js_uses_known_i18n_keys_only` must fail before the markup and JS land and pass after.
  - Add `tests/test_server.py::test_index_has_live_tab`: the page has `id="panel-live"` and `id="tpl-live-moment"`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement the HTML, CSS and JS** as specified above.
- [ ] **Step 4: Run them and see them pass.** Run the whole suite.
- [ ] **Step 5: Verify in the app.**
  1. Start Baitd from source in the preview browser.
  2. Add a live Kick channel from the featured list and a Twitch channel. Mark the Kick one as "es mío".
  3. Start the capture, wait 60 s, press "Marcar momento" and wait for the card.
  4. Open "Revisar", switch the format to Partido, move the cam box and export.
  5. Check that the clip appears in the queue and plays at 1080×1920.
  6. Repeat on the Twitch channel, not marked as the user's: the credit shows the Twitch logo and `@handle`.
  7. Take screenshots of the channel list, the capture box and the moments list, and attach them to the task report.
- [ ] **Step 6: Commit** with the message `feat(ui): En directo tab with channels, capture, marks and review`.

---

### Task 15: Packaging, self-test and closing notes

**Files:**
- Modify:
  - `packaging/baitd.spec`: `hiddenimports` gains `collect_submodules("aiohttp")`, plus `collect_submodules("pynput")` when not on Windows;
  - `pyproject.toml`: check that both new dependencies are present;
  - `src/baitd/selftest.py`: `_check_live()`.
- Create: `docs/decisiones-0.5.0.md`
- Test: `tests/test_packaging.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces:
  - **`selftest._check_live(workdir)`**, offline:
    1. Generate 4 MPEG-TS segments of 2 s with FFmpeg lavfi.
    2. Write a media playlist with program-date-times and parse it with `hls.parse_media_playlist`.
    3. Feed a `RingBuffer` and `cut` 2 s to 6 s into an MP4; check that its probe duration is about 8 s and that it has video.
    4. `import aiohttp`.
    5. On Windows, check that `hotkey.parse_hotkey("F9")` works and that `ctypes.windll.user32.RegisterHotKey` resolves. Do not register it.
    - `run_selftest` calls it.
  - **`docs/decisiones-0.5.0.md`**, in Spanish, in the style of `docs/decisiones-0.3.0.md`:
    - every ruling from R1 to R10 and every ruling made during execution;
    - the calibrated thresholds;
    - what was not reached, for example laughter detected from the audio, or YouTube chat if it failed;
    - the Kick chat API change found on 2026-11-15.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_packaging.py`: the spec text contains `collect_submodules("aiohttp")`, and `pyproject.toml` lists `aiohttp` and `pynput` with the marker `sys_platform != 'win32'`.
  - `tests/test_smoke.py` (or the existing selftest test): `run_selftest` passes, including `_check_live`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the whole suite and the self-test from source.** Run `python -m pytest -m "not network" -q`, then `python -m baitd --selftest`. Both pass.
- [ ] **Step 5: Build the frozen app locally.** Run `pyinstaller packaging/baitd.spec`, then `dist/Baitd/Baitd.exe --selftest`. It passes, and `dist/Baitd` stays under 450 MB.
- [ ] **Step 6: Commit** with the message `build: package live clipping deps, self-test the live path, closing notes`.
