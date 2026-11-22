/* Baitd UI. Plain JS, no build step. Every visible string comes from i18n/<lang>.json via tr(). */
"use strict";

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const BOOT = JSON.parse($("#boot").textContent);
  const KNOWN_LANGS = ["es", "en", "fr", "de"];
  const ACTIVE = ["pending", "running"];
  const FINISHED = ["done", "error", "cancelled"];
  const RETRYABLE = ["error", "cancelled"];
  const SAVE_DELAY = 500;
  const POLL_MS = 1000;
  const MIN_CLIP_S = 10; // clip length limits and clips per run, as the server checks them
  const MAX_CLIP_S = 180;
  const MAX_EXPORT = 20;
  // En directo: the ring buffer's length, the capture states that count as running, the poll rates, and
  // the cam box aspect of the Partido band (1080×768: the renderer scales the box straight into it)
  const BUFFER_S = 300;
  const LIVE_ACTIVE = ["starting", "capturing", "reconnecting"];
  const LIVE_FAST_MS = 1000;
  const LIVE_IDLE_MS = 10000;
  const CHANNELS_MS = 60000;
  const CHANNELS_RECHECK_MS = 5000; // the server checks the channels in the background: ask again soon
  const NOTICE_MS = 20000;
  const NOTICE_WARN_MS = 60000;
  const NOTICE_WARN = ["ended", "error", "disk_full", "save_error", "chat_off", "no_footage", "too_short"];
  const APP_NOTICES = ["no_capture", "hotkey_busy"]; // shown app-wide (#live-alert), whatever tab is open
  const CAM_ASPECT = 1080 / 768;
  // SubtitleStyle.font -> CSS family + weight (declared with @font-face in app.css)
  const FONT_CSS = {
    "Montserrat ExtraBold": ["Montserrat", 800],
    "Inter SemiBold": ["Inter", 600],
    "Inter": ["Inter", 400],
    "Anton": ["Anton", 400],
    "Noto Sans": ["Noto Sans", 400],
  };

  let STR = {};
  const state = {
    profiles: [],
    current: null,
    voices: [],
    music: { moods: [], tracks: [] },
    jobs: [],
    review: null, // [{topic, script, error}]
    busy: false,
    previewId: null,
    pollTimer: null,
    saveTimer: null,
    savePromise: null,
    voiceReq: 0,
    tab: "short",
    clip: {
      source: null, // uploaded file: {source_id, title, duration, has_video, unused}
      chat: null, // a stream's chat file read by the server: {chat_id, messages}
      upload: null, // upload in flight: {id, xhr, cancelled}
      watch: new Set(), // clip_analyze jobs started from this page: their review opens when done
      review: null, // {analysisId, profileId, source, moments: [{...moment, orig, pick, raw}]}
      settingsBad: false, // a typed clip number is out of range (shown in #clip-error, not applied)
      saveFailed: false, // a profile save failed while this tab was shown (also in #clip-error)
    },
    live: {
      ready: false, // strings loaded: before that nothing in the panel is drawn
      sub: "video", // the Clipping sub-tab: "video" or "live"
      shown: false, // the En directo panel is on screen (tab, sub-tab and window visible)
      channels: [],
      channelsTimer: null,
      status: null, // the last GET /api/live/status
      since: null, // the newest event id seen; null until the first poll (older events are not shown)
      statusTimer: null,
      notices: [], // [{id, kind, text, until}]
      noPdt: new Set(), // channels whose capture had no program-date-times: they show the delay field
      moments: [],
      momentsSeq: 0,
      picked: new Set(), // analysis ids ticked for "Exportar seleccionados"
      cardErrors: new Map(), // analysis id -> message shown on that card
      aiPosting: new Set(), // "Título con IA" requests on their way (a double click must not spend two)
      exporting: false,
      jobStatus: new Map(), // clip_export / live_title job id -> status at the last queue poll
      hotkey: "F9",
      rec: null, // the Ajustes hotkey recorder: {before} while it waits for a key
    },
  };

  // ---------- i18n ----------

  function tr(key, vars) {
    let s = Object.prototype.hasOwnProperty.call(STR, key) ? STR[key] : key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
    return s;
  }

  function applyI18n(root = document) {
    const each = (attr, fn) => {
      const list = $$(`[${attr}]`, root);
      if (root.nodeType === 1 && root.hasAttribute(attr)) list.push(root);
      list.forEach(fn);
    };
    each("data-i18n", (el) => { el.textContent = tr(el.dataset.i18n); });
    each("data-i18n-placeholder", (el) => { el.placeholder = tr(el.dataset.i18nPlaceholder); });
    each("data-i18n-aria", (el) => { el.setAttribute("aria-label", tr(el.dataset.i18nAria)); });
    each("data-i18n-title", (el) => { el.title = tr(el.dataset.i18nTitle); });
  }

  function renderVersion() {
    $("#app-version").textContent = tr("settings.version", { v: BOOT.version });
  }

  async function loadStrings(lang) {
    const res = await fetch(`/static/i18n/${encodeURIComponent(lang)}.json`);
    if (!res.ok) throw new Error(tr("err.network"));
    STR = await res.json();
  }

  // ---------- API ----------

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json"; // the server rejects anything else
      opts.body = JSON.stringify(body || {});
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (_) {
      throw new Error(tr("err.network"));
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty or non-JSON body */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || tr("err.network"));
      err.status = res.status;
      throw err;
    }
    return data;
  }

  function showError(el, err) {
    const msg = err ? (err.message || String(err)) : "";
    el.textContent = msg;
    el.hidden = !msg;
  }

  // ---------- profiles ----------

  function profileById(id) {
    return state.profiles.find((p) => p.id === id) || null;
  }

  function renderProfileSelect() {
    const sel = $("#profile");
    sel.replaceChildren(...state.profiles.map((p) => new Option(p.name, p.id)));
    if (state.current) sel.value = state.current.id;
  }

  async function loadProfiles() {
    const { profiles } = await api("GET", "/api/profiles");
    state.profiles = profiles;
    const keep = (state.current && profileById(state.current.id)) || profileById(remembered("profile"));
    state.current = structuredClone(keep || profiles[0]);
    renderProfileSelect();
  }

  // Per-browser convenience only (last chosen profile); the app works without storage.
  function remembered(key) {
    try { return window.localStorage.getItem(`baitd.${key}`); } catch (_) { return null; }
  }

  function remember(key, value) {
    try { window.localStorage.setItem(`baitd.${key}`, value); } catch (_) { /* storage blocked */ }
  }

  function setCurrent(profile) {
    state.current = structuredClone(profile);
    remember("profile", profile.id);
    renderProfileSelect();
    fillForm();
    fillClip();
    loadVoices();
  }

  function scheduleSave() {
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => { state.saveTimer = null; saveNow(); }, SAVE_DELAY);
  }

  // Saves run one after another (chained on state.savePromise): two in flight could finish out
  // of order and leave the older profile on disk.
  function saveNow() {
    const snapshot = structuredClone(state.current);
    const previous = state.savePromise || Promise.resolve();
    const p = previous
      .catch(() => { /* already shown; this save still runs */ })
      .then(() => api("POST", "/api/profiles", snapshot))
      .then((saved) => {
        const i = state.profiles.findIndex((x) => x.id === saved.id);
        if (i >= 0) state.profiles[i] = saved; else state.profiles.push(saved);
        showError($("#form-error"), null);
        if (state.clip.saveFailed) { // the Clipping tab showed this save's failure: it is fixed now
          state.clip.saveFailed = false;
          showError($("#clip-error"), null);
        }
        return saved;
      })
      .catch((e) => {
        showError($("#form-error"), e);
        if (state.tab === "clip") { // its own form error is on the hidden tab
          state.clip.saveFailed = true;
          showError($("#clip-error"), e);
        }
        throw e;
      })
      .finally(() => { if (state.savePromise === p) state.savePromise = null; });
    state.savePromise = p;
    return p;
  }

  async function flushSave() {
    if (state.saveTimer) {
      clearTimeout(state.saveTimer);
      state.saveTimer = null;
      await saveNow();
    } else if (state.savePromise) {
      await state.savePromise;
    }
  }

  async function switchProfile(id) {
    try { await flushSave(); } catch (_) { /* error already shown */ }
    const p = profileById(id);
    if (p) setCurrent(p);
  }

  // Small modal used for names and confirmations (no window.prompt / alert).
  function ask({ title, text = "", value = null, ok, validate }) {
    const dlg = $("#ask");
    $("#ask-title").textContent = title;
    $("#ask-text").textContent = text;
    $("#ask-text").hidden = !text;
    $("#ask-field").hidden = value === null;
    $("#ask-ok").textContent = ok;
    showError($("#ask-error"), null);
    const input = $("#ask-input");
    input.value = value || "";
    return new Promise((resolve) => {
      const done = (result) => {
        cleanup();
        if (dlg.open) dlg.close();
        resolve(result);
      };
      const onOk = () => {
        const v = value === null ? true : input.value.trim();
        const problem = validate ? validate(v) : null;
        if (problem) { showError($("#ask-error"), new Error(problem)); input.focus(); return; }
        done(v);
      };
      const onKey = (e) => { if (e.key === "Enter" && value !== null) { e.preventDefault(); onOk(); } };
      const onCancel = () => done(null);
      const cleanup = () => {
        $("#ask-ok").removeEventListener("click", onOk);
        $("#ask-cancel").removeEventListener("click", onCancel);
        dlg.removeEventListener("cancel", onCancel);
        input.removeEventListener("keydown", onKey);
      };
      $("#ask-ok").addEventListener("click", onOk);
      $("#ask-cancel").addEventListener("click", onCancel);
      dlg.addEventListener("cancel", onCancel);
      input.addEventListener("keydown", onKey);
      dlg.showModal();
      (value === null ? $("#ask-ok") : input).focus();
      if (value !== null) input.select();
    });
  }

  const nameRequired = (v) => (v ? null : tr("err.name"));

  async function profileAction(act) {
    closeProfileMenu();
    try { await flushSave(); } catch (_) { return; }
    const cur = state.current;
    try {
      if (act === "new" || act === "duplicate") {
        const suggested = act === "new" ? tr("profile.default_new") : tr("profile.copy_of", { name: cur.name });
        const name = await ask({ title: tr(act === "new" ? "profile.new" : "profile.duplicate"),
          value: suggested, ok: tr("btn.save"), validate: nameRequired });
        if (!name) return;
        const base = act === "new" ? profileById("default") || cur : cur;
        const draft = structuredClone(base);
        delete draft.id;
        draft.name = name;
        const saved = await api("POST", "/api/profiles", draft);
        state.profiles.push(saved);
        setCurrent(saved);
      } else if (act === "rename") {
        const name = await ask({ title: tr("profile.rename"), value: cur.name, ok: tr("btn.save"), validate: nameRequired });
        if (!name) return;
        cur.name = name;
        await saveNow();
        renderProfileSelect();
      } else if (act === "delete") {
        if (state.profiles.length <= 1) throw new Error(tr("profile.last"));
        const yes = await ask({ title: tr("profile.delete"), text: tr("profile.confirm_delete", { name: cur.name }),
          ok: tr("btn.delete") });
        if (!yes) return;
        const { profiles } = await api("DELETE", `/api/profiles/${encodeURIComponent(cur.id)}`, {});
        state.profiles = profiles;
        setCurrent(profiles[0]);
      }
      showError($("#top-error"), null);
    } catch (e) {
      showError($("#top-error"), e);
    }
  }

  function openProfileMenu() {
    const menu = $("#profile-menu");
    menu.hidden = false;
    $("#profile-menu-btn").setAttribute("aria-expanded", "true");
    const del = $('[data-profile-act="delete"]', menu);
    del.disabled = state.profiles.length <= 1;
    $("button:not([disabled])", menu).focus();
  }

  function closeProfileMenu(focusButton = false) {
    const menu = $("#profile-menu");
    if (menu.hidden) return;
    menu.hidden = true;
    $("#profile-menu-btn").setAttribute("aria-expanded", "false");
    if (focusButton) $("#profile-menu-btn").focus();
  }

  function onMenuKey(e) {
    const items = $$("#profile-menu button:not([disabled])");
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); closeProfileMenu(true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    else if (e.key === "Tab") closeProfileMenu();
  }

  // ---------- form <-> profile ----------

  function langCode(p) {
    return (p.language || "").trim().toLowerCase();
  }

  function fillForm() {
    const p = state.current;
    if (!p) return;
    const code = langCode(p);
    const known = KNOWN_LANGS.includes(code);
    $("#f-lang").value = known ? code : "other";
    $("#f-lang-other").value = known ? "" : p.language;
    $("#lang-other-box").hidden = known;
    $("#f-aspect").value = p.aspect;
    $("#f-fps").value = String(p.fps);
    $("#f-seconds").value = String(p.seconds);
    $("#f-tts").value = p.tts_provider;
    $("#f-rate").value = String(p.rate);
    fillSubtitle("f");
    renderMusicSelect();
    $("#f-volume").value = String(Math.round(p.music_volume * 100));
    $("#f-tone").value = p.tone;
    renderDerived();
  }

  function readForm(target) {
    const p = state.current;
    const langSel = $("#f-lang").value;
    $("#lang-other-box").hidden = langSel !== "other";
    p.language = langSel === "other" ? $("#f-lang-other").value.trim() : langSel;
    p.aspect = $("#f-aspect").value;
    p.fps = Number($("#f-fps").value);
    p.seconds = Number($("#f-seconds").value);
    p.tts_provider = $("#f-tts").value;
    if ($("#f-voice").value) p.voice = $("#f-voice").value;
    p.rate = parseInt($("#f-rate").value, 10) || 0;
    readSubtitle("f", target);
    p.music = $("#f-music").value || "none";
    p.music_volume = Math.round(Number($("#f-volume").value)) / 100;
    p.tone = $("#f-tone").value;
  }

  // The subtitle controls exist in both panels (prefix "f" in Crear short, "c" in Clipping); both edit
  // the same profile.subtitle.
  function fillSubtitle(prefix) {
    const s = state.current.subtitle;
    $(`#${prefix}-preset`).value = s.preset;
    $(`#${prefix}-size`).value = String(s.size);
    $(`#${prefix}-color`).value = s.color.toLowerCase();
    $(`#${prefix}-highlight`).value = s.highlight.toLowerCase();
    $(`#${prefix}-position`).value = s.position;
  }

  function readSubtitle(prefix, target) {
    const p = state.current;
    if (target && target.id === `${prefix}-preset`) {
      p.subtitle = structuredClone(BOOT.presets[$(`#${prefix}-preset`).value]);
      fillSubtitle(prefix);
    } else {
      const size = parseInt($(`#${prefix}-size`).value, 10);
      if (size >= 24 && size <= 200) p.subtitle.size = size;
      p.subtitle.color = $(`#${prefix}-color`).value.toUpperCase();
      p.subtitle.highlight = $(`#${prefix}-highlight`).value.toUpperCase();
      p.subtitle.position = $(`#${prefix}-position`).value;
    }
  }

  function onFormInput(e) {
    if (!state.current) return;
    const t = e.target;
    if (!t.closest || !t.closest(".form") || t.closest("#review")) return;
    if (!t.matches("select, input:not([type=radio]):not([type=checkbox]):not(#topic), textarea:not(#topics)")) return;
    readForm(t);
    renderDerived();
    fillClip(); // format, fps and subtitles are shared with the Clipping tab
    if (t.id === "f-lang" && t.value === "other") $("#f-lang-other").focus();
    if (state.current.language) scheduleSave();
    else if (t.id !== "f-lang-other") showError($("#form-error"), new Error(tr("err.lang")));
    if (e.type === "change" && ["f-tts", "f-lang", "f-lang-other"].includes(t.id)) loadVoices();
  }

  function renderDerived() {
    const p = state.current;
    $("#o-rate").textContent = `${p.rate > 0 ? "+" : ""}${p.rate} %`;
    $("#o-volume").textContent = `${Math.round(p.music_volume * 100)} %`;
    $("#voice-warning").hidden = p.tts_provider !== "edge";
    const noSpeed = p.tts_provider === "elevenlabs"; // its timestamps endpoint has no speed control
    $("#f-rate").disabled = noSpeed;
    $("#rate-note").hidden = !noSpeed;
    $("#fps-hint").hidden = p.fps !== 60;
    $("#phone").dataset.aspect = p.aspect;
    renderSummaries();
    renderSampleSub();
  }

  function voiceShortName(id) {
    const m = /^([a-z]{2,3}-[A-Za-z]{2,4})-(.+?)(?:Multilingual)?Neural$/.exec(id || "");
    return m ? m[2] : null;
  }

  function voiceLabel(v) {
    const m = /^([a-z]{2,3}-[A-Za-z]{2,4})-(.+?)(?:Multilingual)?Neural$/.exec(v.id);
    return m ? `${m[2]} · ${m[1]}` : v.name;
  }

  function renderSummaries() {
    const p = state.current;
    const voice = state.voices.find((v) => v.id === p.voice);
    const vname = voiceShortName(p.voice) || (voice && voice.name) || p.voice;
    const provider = p.tts_provider === "edge" ? tr("voice.free") : tr(`voice.${p.tts_provider}`);
    const sumVoice = $("#sum-voice");
    sumVoice.replaceChildren(document.createTextNode(`${vname} · ${provider}`));
    if (p.tts_provider === "edge") {
      const w = document.createElement("span");
      w.className = "warn";
      w.textContent = ` ${tr("voice.summary_warn")}`;
      sumVoice.append(w);
    }
    $("#sum-subs").textContent = `${tr(`preset.${p.subtitle.preset}`)} · ${tr(`pos.${p.subtitle.position}`)}`;
    $("#sum-music").textContent = `${musicLabel(p.music)} · ${Math.round(p.music_volume * 100)} %`;
    $("#sum-ai").textContent = p.tone.trim() || tr("ai.sum_default");
  }

  function renderSampleSub() {
    const s = state.current.subtitle;
    const sub = $("#sample-sub");
    const [family, weight] = FONT_CSS[s.font] || ["Noto Sans", 400];
    sub.style.setProperty("--sub-font", `"${family}"`);
    sub.style.setProperty("--sub-weight", String(weight));
    sub.style.setProperty("--sub-color", s.color);
    sub.style.setProperty("--sub-hl", s.highlight);
    sub.style.setProperty("--sub-size", String(s.size));
    sub.style.setProperty("--out-w", state.current.aspect === "16:9" ? "1920" : "1080");
    sub.dataset.pos = s.position;
    sub.dataset.box = String(Boolean(s.box));
    sub.dataset.upper = String(s.preset === "poster");
  }

  // ---------- music ----------

  function musicLabel(value) {
    if (!value || value === "none") return tr("music.none");
    if (state.music.moods.includes(value)) return tr(`mood.${value}`);
    const t = state.music.tracks.find((x) => x.value === value);
    return t ? t.title : value;
  }

  async function loadMusic() {
    state.music = await api("GET", "/api/music");
  }

  function renderMusicSelect() {
    const sel = $("#f-music");
    const none = new Option(tr("music.none"), "none");
    const moods = document.createElement("optgroup");
    moods.label = tr("music.moods");
    state.music.moods.forEach((m) => moods.append(new Option(tr(`mood.${m}`), m)));
    const tracks = document.createElement("optgroup");
    tracks.label = tr("music.tracks");
    state.music.tracks.forEach((t) => tracks.append(new Option(`${t.title} · ${tr(`mood.${t.mood}`)}`, t.value)));
    sel.replaceChildren(none, moods, ...(state.music.tracks.length ? [tracks] : []));
    sel.value = state.current ? state.current.music : "none";
    if (!sel.value) sel.value = "none";
  }

  // ---------- voices ----------

  async function loadVoices() {
    const p = state.current;
    const sel = $("#f-voice");
    const lang = langCode(p).split(/[-_]/)[0];
    const req = ++state.voiceReq;
    showError($("#voice-error"), null);
    if (!lang) return;
    sel.disabled = true;
    sel.replaceChildren(new Option(tr("voice.loading"), ""));
    try {
      const url = `/api/voices?provider=${encodeURIComponent(p.tts_provider)}&lang=${encodeURIComponent(lang)}`;
      const { voices } = await api("GET", url);
      if (req !== state.voiceReq) return;
      state.voices = voices;
      if (!voices.length) {
        sel.replaceChildren(new Option(tr("voice.none"), ""));
      } else {
        sel.replaceChildren(...voices.map((v) => new Option(voiceLabel(v), v.id)));
        if (!voices.some((v) => v.id === p.voice)) {
          p.voice = voices[0].id;
          scheduleSave();
        }
        sel.value = p.voice;
        sel.disabled = false;
      }
    } catch (e) {
      if (req !== state.voiceReq) return;
      state.voices = [];
      sel.replaceChildren(new Option(voiceShortName(p.voice) || p.voice || tr("voice.none"), p.voice || ""));
      sel.disabled = false;
      showError($("#voice-error"), e);
    }
    renderSummaries();
  }

  // ---------- topics, review and create ----------

  function mode() {
    return $("#mode-batch").checked ? "batch" : "single";
  }

  function readTopics() {
    if (mode() === "single") {
      const t = $("#topic").value.trim();
      if (!t) throw new Error(tr("err.topic"));
      return [t];
    }
    const list = $("#topics").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!list.length) throw new Error(tr("err.topics"));
    return [...new Set(list)];
  }

  function setBusy(busy) {
    state.busy = busy;
    $("#write-script").disabled = busy;
    $("#create").disabled = busy;
    $$("#review-list .script-retry, #review-list .script-remove").forEach((b) => { b.disabled = busy; });
    $("#clip-find").disabled = busy;
    $("#clip-create").disabled = busy;
    $("#c-file-btn").disabled = busy;
    $("#c-chat-btn").disabled = busy;
    $("#clip-discard").disabled = busy;
    $$("#moment-list .m-remove").forEach((b) => { b.disabled = busy; });
    renderExportState();
    $("#profile").disabled = busy; // a batch must be submitted under the profile that wrote it
    $("#profile-menu-btn").disabled = busy;
    if (busy) closeProfileMenu();
    $(".form").setAttribute("aria-busy", String(busy));
    $(".form-clip").setAttribute("aria-busy", String(busy));
  }

  async function writeScripts(topics) {
    setBusy(true);
    state.review = topics.map((topic) => ({ topic, script: null, error: "" }));
    renderReview();
    $("#review").hidden = false;
    for (let i = 0; i < state.review.length; i++) {
      $("#review-progress").textContent = tr("review.progress", { n: i + 1, total: state.review.length });
      await generateOne(state.review[i]);
      renderReview();
    }
    const ok = state.review.filter((r) => r.script).length;
    $("#review-progress").textContent = state.review.length > 1 ? tr("review.done", { n: ok, total: state.review.length }) : "";
    setBusy(false);
    const first = $("#review-list .scene-text, #review-list .script-retry:not([hidden])");
    if (first) first.focus();
  }

  async function generateOne(item) {
    try {
      item.script = await api("POST", "/api/script", { topic: item.topic, profile_id: state.current.id });
      item.error = "";
    } catch (e) {
      item.script = null;
      item.error = e.message;
    }
  }

  function renderReview() {
    const list = $("#review-list");
    if (!state.review) {
      list.replaceChildren();
      $("#review").hidden = true;
      return;
    }
    list.replaceChildren(...state.review.map((item, i) => scriptCard(item, i)));
  }

  function scriptCard(item, idx) {
    const node = $("#tpl-script").content.firstElementChild.cloneNode(true);
    applyI18n(node);
    $(".script-topic", node).textContent = item.topic;
    const err = $(".script-err", node);
    showError(err, item.error ? new Error(item.error) : null);
    const retry = $(".script-retry", node);
    retry.hidden = !item.error;
    retry.disabled = state.busy;
    retry.addEventListener("click", async () => {
      setBusy(true);
      retry.disabled = true;
      await generateOne(item);
      setBusy(false);
      renderReview();
    });
    const remove = $(".script-remove", node);
    remove.disabled = state.busy;
    remove.setAttribute("aria-label", `${tr("review.remove")}: ${item.topic}`);
    remove.addEventListener("click", () => removeReviewItem(item));
    const body = $(".script-body", node);
    body.hidden = !item.script;
    if (!item.script) return node;
    // no separate hook field: the hook is the first sentence of scene 1 (the server re-derives it)
    const scenes = $(".scenes", node);
    item.script.scenes.forEach((scene, n) => {
      const li = $("#tpl-scene").content.firstElementChild.cloneNode(true);
      applyI18n(li);
      $(".scene-n", li).textContent = tr("review.scene", { n: n + 1 });
      const text = $(".scene-text", li);
      const kw = $(".scene-kw", li);
      text.id = `scene-${idx}-${n}`;
      kw.id = `kw-${idx}-${n}`;
      $(".scene-text-label", li).htmlFor = text.id;
      $(".scene-kw-label", li).htmlFor = kw.id;
      text.value = scene.text;
      kw.value = scene.keywords.join(", ");
      text.addEventListener("input", () => { scene.text = text.value; });
      kw.addEventListener("input", () => {
        scene.keywords = kw.value.split(",").map((k) => k.trim()).filter(Boolean);
      });
      scenes.append(li);
    });
    return node;
  }

  // I8: drop one topic from the batch review; the last one closes the review (batch stays idle)
  function removeReviewItem(item) {
    if (!state.review || state.busy) return;
    const i = state.review.indexOf(item);
    if (i < 0) return;
    state.review.splice(i, 1);
    if (!state.review.length) {
      state.review = null;
      $("#review-progress").textContent = "";
      renderReview();
      $("#write-script").focus();
      return;
    }
    renderReview();
    const cards = $$("#review-list .script-remove");
    (cards[Math.min(i, cards.length - 1)] || $("#create")).focus();
  }

  function reviewedScripts() {
    const scripts = {};
    for (const item of state.review) {
      if (!item.script) continue; // failed ones are written by the queue itself
      const bad = item.script.scenes.some((s) => !s.text.trim() || !s.keywords.length);
      if (bad) throw new Error(tr("err.scene"));
      scripts[item.topic] = item.script;
    }
    return scripts;
  }

  async function onWrite() {
    showError($("#form-error"), null);
    try {
      const topics = readTopics();
      await flushSave();
      await writeScripts(topics);
    } catch (e) {
      setBusy(false);
      showError($("#form-error"), e);
    }
  }

  async function onCreate() {
    showError($("#form-error"), null);
    $("#form-status").textContent = "";
    try {
      let topics;
      let scripts = {};
      if (state.review) {
        topics = state.review.map((r) => r.topic);
        scripts = reviewedScripts();
      } else {
        topics = readTopics();
        if (mode() === "batch" && $("#review-first").checked) {
          await flushSave();
          await writeScripts(topics);
          return;
        }
      }
      setBusy(true);
      await flushSave();
      const { ids } = await api("POST", "/api/jobs", { topics, profile_id: state.current.id, scripts });
      $("#form-status").textContent = tr("queue.added", { n: ids.length });
      state.review = null;
      renderReview();
      if (mode() === "single") $("#topic").value = "";
      refreshJobs();
    } catch (e) {
      showError($("#form-error"), e);
    } finally {
      setBusy(false);
    }
  }

  // ---------- side menu ----------

  // One menu, three views: Clips and En vivo share #panel-clip; Short con IA has #panel-short.
  // state.tab ("short"/"clip") and state.live.sub ("video"/"live") keep their meaning for the rest of the app.
  function setView(view, focus = false) {
    if (view !== "live" && view !== "short") view = "clips";
    state.tab = view === "short" ? "short" : "clip";
    state.live.sub = view === "live" ? "live" : "video";
    $$('.navtabs [role="tab"]').forEach((b) => {
      const on = b.dataset.view === view;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    $("#panel-short").hidden = view !== "short";
    $("#panel-clip").hidden = view === "short";
    $("#panel-clip").setAttribute("aria-labelledby", `nav-${view === "live" ? "live" : "clips"}`);
    $("#side").hidden = view !== "short";
    $("#panel-video").hidden = view !== "clips";
    $("#panel-live").hidden = view !== "live";
    $(".skip").setAttribute("href", view === "short" ? "#head" : "#clip-head");
    remember("view", view);
    syncLiveShown();
  }

  // Saved view, or the one implied by the old tab/subtab values.
  function savedView() {
    const v = remembered("view");
    if (v === "clips" || v === "live" || v === "short") return v;
    if (remembered("tab") === "short") return "short";
    return remembered("tab") === "clip" && remembered("subtab") === "live" ? "live" : "clips";
  }

  // Arrow up/down, Home and End move between the menu buttons.
  function onTabKey(e) {
    const tabs = $$('[role="tab"]', e.currentTarget);
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    const next = {
      ArrowDown: tabs[(i + 1) % tabs.length],
      ArrowUp: tabs[(i - 1 + tabs.length) % tabs.length],
      Home: tabs[0],
      End: tabs[tabs.length - 1],
    }[e.key];
    if (!next) return;
    e.preventDefault();
    setView(next.dataset.view, true);
  }

  // ---------- clipping: settings (profile.clipping, plus the shared aspect, fps and subtitle) ----------

  const CLIP_LANGS = ["auto", "es", "en", "fr", "de", "it", "pt"];

  function fillClip() {
    const p = state.current;
    if (!p) return;
    const c = p.clipping;
    $("#c-count").value = String(c.count || MAX_EXPORT);
    $("#c-count-all").checked = c.count === 0;
    $("#c-count").disabled = c.count === 0;
    $("#c-min").value = String(c.min_s);
    $("#c-max").value = String(c.max_s);
    $("#c-kind").value = c.kind;
    $("#c-aspect").value = p.aspect;
    $("#live-aspect").value = p.aspect;
    $("#c-fps").value = String(p.fps);
    $("#c-transcriber").value = c.transcriber;
    $("#c-model").value = c.whisper_model;
    const lang = $("#c-language");
    $$("option[data-extra]", lang).forEach((o) => o.remove());
    if (!CLIP_LANGS.includes(c.language)) { // a code saved by hand stays selectable
      const o = new Option(c.language, c.language);
      o.dataset.extra = "1";
      lang.append(o);
    }
    lang.value = c.language;
    $("#c-framing").value = c.framing;
    fillSubtitle("c");
    $("#c-hook").checked = c.hook_title;
    $("#c-credit").checked = c.credit;
    $("#c-music").checked = c.music;
    if (state.clip.settingsBad) { // the typed numbers were replaced by the profile's valid ones
      state.clip.settingsBad = false;
      showError($("#clip-error"), null);
    }
    syncPills();
    renderClipDerived();
  }

  function renderClipDerived() {
    const p = state.current;
    if (!p) return;
    $("#c-model").disabled = p.clipping.transcriber !== "local";
    $("#c-fps-hint").hidden = p.fps !== 60;
  }

  // A row of aria-pressed buttons, one per <option>, shown instead of the (hidden) list.
  function pillFor(select) {
    const group = document.createElement("div");
    group.className = "pills";
    group.setAttribute("role", "group");
    const lbl = select.labels[0];
    if (lbl) {
      lbl.id = lbl.id || `${select.id}-label`;
      group.setAttribute("aria-labelledby", lbl.id);
    }
    for (const o of select.options) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pill";
      b.dataset.value = o.value;
      b.dataset.i18n = o.dataset.i18n;
      b.textContent = o.textContent;
      b.addEventListener("click", () => {
        select.value = o.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      group.append(b);
    }
    select.hidden = true;
    select.after(group);
    return group;
  }

  // The pill groups follow their list; the duration and count pills show the profile's values.
  function syncPills() {
    $$(".pills[role=group]").forEach((g) => {
      const v = g.previousElementSibling.value;
      $$("button", g).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.value === v)));
    });
    const c = state.current && state.current.clipping;
    if (!c) return;
    $("#c-len-text").textContent = tr("clip.len_pill", { min: c.min_s, max: c.max_s });
    $("#c-count-text").textContent = c.count === 0 ? tr("clip.count_all")
      : c.count === 1 ? tr("clip.count_pill_one") : tr("clip.count_pill", { n: c.count });
  }

  // Duration and count pills: a small panel under the pill; Esc or a click outside closes it.
  function togglePop(pill, open) {
    pill.setAttribute("aria-expanded", String(open));
    document.getElementById(pill.getAttribute("aria-controls")).hidden = !open;
  }

  function bindPops() {
    const pills = [$("#c-len-pill"), $("#c-count-pill")];
    pills.forEach((p) => p.addEventListener("click", () => {
      const open = p.getAttribute("aria-expanded") !== "true";
      pills.forEach((q) => togglePop(q, false));
      togglePop(p, open);
    }));
    document.addEventListener("click", (e) => {
      pills.forEach((p) => { if (!p.parentElement.contains(e.target)) togglePop(p, false); });
    });
    pills.forEach((p) => p.parentElement.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || p.getAttribute("aria-expanded") !== "true") return;
      e.preventDefault();
      togglePop(p, false);
      p.focus();
    }));
  }

  function openOptions() {
    $("#c-options").showModal();
  }

  function closeOptions() {
    $("#c-options").close();
  }

  // Reads the Clipping controls into the profile. Out-of-range numbers are not applied; returns the
  // problem ("cause → fix") or "".
  function readClip(target) {
    const p = state.current;
    const c = p.clipping;
    p.aspect = $("#c-aspect").value;
    p.fps = Number($("#c-fps").value);
    readSubtitle("c", target);
    c.kind = $("#c-kind").value;
    c.transcriber = $("#c-transcriber").value;
    c.whisper_model = $("#c-model").value;
    c.language = $("#c-language").value;
    c.framing = $("#c-framing").value;
    c.hook_title = $("#c-hook").checked;
    c.credit = $("#c-credit").checked;
    c.music = $("#c-music").checked;
    const int = (sel) => (/^\d+$/.test($(sel).value.trim()) ? Number($(sel).value) : NaN);
    const all = $("#c-count-all").checked; // every good moment found, up to MAX_EXPORT
    $("#c-count").disabled = all;
    const count = all ? 0 : int("#c-count");
    const min = int("#c-min");
    const max = int("#c-max");
    if (!all && !(count >= 1 && count <= MAX_EXPORT)) return tr("err.clip_count");
    if (!(min >= MIN_CLIP_S && max <= MAX_CLIP_S && min <= max)) return tr("err.clip_len");
    Object.assign(c, { count, min_s: min, max_s: max });
    return "";
  }

  function onClipInput(e) {
    if (!state.current) return;
    const t = e.target;
    if (!t.closest || !t.closest("#clip-settings") || !t.matches("select, input")) return;
    const problem = readClip(t);
    if (problem) showError($("#clip-error"), new Error(problem));
    else if (state.clip.settingsBad) showError($("#clip-error"), null);
    state.clip.settingsBad = Boolean(problem);
    fillForm(); // the Crear short tab shows the same format, fps and subtitles
    syncPills();
    renderClipDerived();
    if (state.current.language) scheduleSave(); // the profile only ever holds valid numbers
  }

  // ---------- clipping: times ----------

  function fmtTime(sec, tenths = true) {
    const total = tenths ? Math.round(sec * 10) / 10 : Math.round(sec);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total - h * 3600) / 60);
    const s = total - h * 3600 - m * 60;
    const ss = `${s < 10 ? "0" : ""}${Number.isInteger(s) ? s : s.toFixed(1)}`;
    return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
  }

  // "1:05", "1:05.5", "0:01:05" or plain seconds ("65,5"); NaN when it is none of those.
  function parseTime(text) {
    const parts = String(text).trim().replace(",", ".").split(":");
    if (parts.length > 3 || !parts.every((x, i) => (i === parts.length - 1 ? /^\d+(\.\d+)?$/ : /^\d+$/).test(x))) {
      return NaN;
    }
    if (parts.slice(1).some((x) => Number(x) >= 60)) return NaN;
    return parts.reduce((acc, x) => acc * 60 + Number(x), 0);
  }

  // ---------- clipping: source (a link or an uploaded file) ----------

  function setUploadPct(pct) {
    const v = Math.max(0, Math.min(100, Math.round(pct)));
    $("#upload-fill").style.width = `${v}%`;
    $("#upload-bar").setAttribute("aria-valuenow", String(v));
    $("#upload-pct").textContent = v >= 100 ? tr("clip.upload_checking") : `${v} %`;
  }

  function setSource(data) {
    state.clip.source = data;
    $("#c-source-title").textContent = `${data.title} · ${fmtTime(data.duration, false)}`;
    $("#c-source").hidden = false;
    $("#c-url").value = "";
    $("#c-url").disabled = true; // one source at a time: the file wins until it is removed
  }

  function clearSource() {
    state.clip.source = null;
    $("#c-source").hidden = true;
    $("#c-url").disabled = false;
  }

  // A finished stream's chat (TwitchDownloader's JSON): its peaks pick the moments instead of the AI.
  function setChat(chat) {
    state.clip.chat = chat;
    $("#c-chat").hidden = !chat;
    if (chat) $("#c-chat-title").textContent = tr("clip.chat_loaded", { n: chat.messages.toLocaleString() });
  }

  async function readChat(file) {
    if (!file) return;
    showError($("#clip-error"), null);
    $("#c-chat").hidden = false;
    $("#c-chat-title").textContent = tr("clip.chat_reading");
    try {
      setChat(await api("POST", "/api/chats", { text: await file.text() }));
    } catch (e) {
      setChat(null);
      showError($("#clip-error"), e);
    }
  }

  // An upload no job has used yet is also deleted on the server; the server refuses (409) if something needs it.
  function dropSource() {
    const s = state.clip.source;
    clearSource();
    if (!s || !s.unused) return;
    api("DELETE", `/api/sources/${encodeURIComponent(s.source_id)}`, {}).catch((e) => {
      if (e.status !== 404 && e.status !== 409) showError($("#clip-error"), e);
    });
  }

  // The server takes at most 256 MiB per PUT; smaller chunks keep a retry cheap.
  const UPLOAD_CHUNK = 64 * 1024 * 1024;

  function uploadExtOk(name) {
    const m = /\.([^./\\]+)$/.exec(name || "");
    return Boolean(m) && BOOT.upload_exts.includes(m[1].trim().toLowerCase());
  }

  // One chunk as raw bytes over XMLHttpRequest (fetch has no upload progress). Resolves with the server's
  // {offset}; an abort rejects with err.aborted set.
  function putChunk(up, offset, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      up.xhr = xhr;
      xhr.open("PUT", `/api/uploads/${encodeURIComponent(up.id)}?offset=${offset}`);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-Baitd-Upload", "1");
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (e) => onProgress(e.loaded));
      xhr.addEventListener("load", () => {
        up.xhr = null;
        const data = xhr.response;
        if (xhr.status >= 200 && xhr.status < 300 && data && Number.isInteger(data.offset)) resolve(data);
        else reject(new Error((data && data.error) || tr("err.upload")));
      });
      xhr.addEventListener("error", () => { up.xhr = null; reject(new Error(tr("err.network"))); });
      xhr.addEventListener("abort", () => {
        up.xhr = null;
        const err = new Error(tr("clip.upload_cancelled"));
        err.aborted = true;
        reject(err);
      });
      xhr.send(blob);
    });
  }

  // Chunked upload: begin (name, size: the server checks both before storing anything), 64 MiB PUTs in
  // order, then finish (probe). Cancel aborts the chunk in flight and deletes the partial upload.
  async function startUpload(file) {
    if (!file || state.busy || state.clip.upload) return;
    showError($("#clip-error"), null);
    $("#clip-status").textContent = "";
    if (!uploadExtOk(file.name)) {
      showError($("#clip-error"), new Error(tr("err.upload_type")));
      return;
    }
    dropSource();
    const up = { id: "", xhr: null, cancelled: false };
    state.clip.upload = up;
    $("#upload-name").textContent = file.name;
    setUploadPct(0);
    $("#upload-box").hidden = false;
    setBusy(true);
    $("#upload-cancel").focus();
    const cancelled = () => { const err = new Error(tr("clip.upload_cancelled")); err.aborted = true; return err; };
    try {
      up.id = (await api("POST", "/api/uploads", { name: file.name, size: file.size })).upload_id;
      let offset = 0;
      while (offset < file.size) {
        if (up.cancelled) throw cancelled();
        const start = offset;
        const blob = file.slice(start, Math.min(file.size, start + UPLOAD_CHUNK));
        offset = (await putChunk(up, start, blob, (loaded) => setUploadPct(((start + loaded) / file.size) * 100))).offset;
      }
      if (up.cancelled) throw cancelled();
      setUploadPct(100); // "checking the video" while the server probes it
      const data = await api("POST", `/api/uploads/${encodeURIComponent(up.id)}/finish`, {});
      if (up.cancelled) { // too late to abort the probe: the new source goes instead
        api("DELETE", `/api/sources/${encodeURIComponent(data.source_id)}`, {}).catch(() => {});
        throw cancelled();
      }
      setSource({ ...data, unused: true });
    } catch (e) {
      // the partial upload goes on the server (404 when finish already removed it, or it never began)
      if (up.id) api("DELETE", `/api/uploads/${encodeURIComponent(up.id)}`, {}).catch(() => {});
      if (up.cancelled || e.aborted) {
        $("#clip-status").textContent = tr("clip.upload_cancelled");
        $("#c-file-btn").focus();
      } else {
        showError($("#clip-error"), e);
      }
    } finally {
      state.clip.upload = null;
      $("#upload-box").hidden = true;
      setBusy(false);
    }
  }

  function cancelUpload() {
    const up = state.clip.upload;
    if (!up) return;
    up.cancelled = true;
    if (up.xhr) up.xhr.abort();
  }

  function bindDrop() {
    const zone = $("#drop");
    const hasFiles = (e) => Boolean(e.dataTransfer) && Array.from(e.dataTransfer.types || []).includes("Files");
    zone.addEventListener("dragover", (e) => {
      e.preventDefault();
      zone.classList.add("over");
    });
    zone.addEventListener("dragleave", (e) => {
      if (!zone.contains(e.relatedTarget)) zone.classList.remove("over");
    });
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("over");
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) {
        startUpload(file);
        return;
      }
      const link = (e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "")
        .split(/\r?\n/)[0].trim();
      if (/^https?:\/\//i.test(link) && !state.clip.source) $("#c-url").value = link;
    });
    // a file dropped next to the zone must not make the browser leave the app to show it
    window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
    window.addEventListener("drop", (e) => { if (hasFiles(e)) e.preventDefault(); });
  }

  // ---------- clipping: run ----------

  // Which profile started each analysis, so its export uses that same profile (kept across reloads
  // when storage works; otherwise the job's profile name, then the selected profile).
  function analysisProfiles() {
    try {
      const v = JSON.parse(remembered("analyses") || "{}");
      return v && typeof v === "object" ? v : {};
    } catch (_) {
      return {};
    }
  }

  function rememberAnalysisProfile(jobId, profileId) {
    const map = analysisProfiles();
    map[jobId] = profileId;
    const ids = Object.keys(map);
    ids.slice(0, Math.max(0, ids.length - 50)).forEach((k) => { delete map[k]; });
    remember("analyses", JSON.stringify(map));
  }

  function analysisProfileId(job) {
    const known = analysisProfiles()[job.id];
    if (known && profileById(known)) return known;
    const byName = state.profiles.find((p) => p.name === job.profile_name);
    return (byName || state.current).id;
  }

  async function onClipRun(mode) {
    if (state.busy || !state.current) return;
    showError($("#clip-error"), null);
    $("#clip-status").textContent = "";
    const body = { profile_id: state.current.id, mode };
    const url = $("#c-url").value.trim();
    try {
      const problem = readClip(null);
      state.clip.settingsBad = Boolean(problem);
      if (problem) throw new Error(problem);
      if (state.clip.chat) body.chat_id = state.clip.chat.chat_id;
      if (state.clip.source) body.source_id = state.clip.source.source_id;
      else if (!url) throw new Error(tr("err.clip_source"));
      else body.url = url; // the server fixes "youtu.be/x" or "Mira este video https://…" and says if it is no link
      body.overrides = { ...state.current.clipping }; // what the screen shows, even if the save lags or failed
      setBusy(true);
      await flushSave();
      const { job_id: jobId } = await api("POST", "/api/clips", body);
      if (mode === "review") {
        state.clip.watch.add(jobId);
        rememberAnalysisProfile(jobId, state.current.id);
      }
      $("#clip-status").textContent = tr(mode === "review" ? "clip.analyzing" : "clip.added");
      $("#c-url").value = "";
      clearSource();
      setChat(null);
      refreshJobs();
    } catch (e) {
      showError($("#clip-error"), e);
    } finally {
      setBusy(false);
    }
  }

  // ---------- clipping: review list ----------

  // An analysis started here opens its review list when it finishes (unless one is already open).
  function checkAnalyses() {
    state.jobs.forEach((job) => {
      if (!state.clip.watch.has(job.id) || !FINISHED.includes(job.status)) return;
      state.clip.watch.delete(job.id);
      if (job.status === "done" && job.analysis_id && !state.clip.review) openReview(job, false);
    });
  }

  const reviewSignature = (r) => JSON.stringify(r.moments.map((m) => [m.orig, m.pick, m.title, m.start, m.end, m.framing, m.credit, m.cam]));
  const reviewEdited = (r) => reviewSignature(r) !== r.baseline;

  // `job` is a queue item with an analysis_id, or {analysis_id, live} for a moment of the En directo list
  // (`live` is its list entry: the card then shows its reason instead of a score).
  async function openReview(job, userAsked) {
    const errBox = userAsked ? (job.live ? $("#live-moments-error") : $("#queue-error")) : $("#clip-error");
    const open = state.clip.review;
    if (!open || open.analysisId !== job.analysis_id) {
      if (open && userAsked && reviewEdited(open)) {
        const yes = await ask({ title: tr("clip.switch_title"), text: tr("clip.switch_confirm"), ok: tr("queue.review") });
        if (!yes) return;
      }
      try {
        const data = await api("GET", `/api/analyses/${encodeURIComponent(job.analysis_id)}`);
        const profileId = analysisProfileId(job);
        const count = (profileById(profileId) || state.current).clipping.count;
        // orig: the index in the analysis, so the server keeps its texts even after the bounds are edited
        const ranked = data.moments.map((m, orig) => ({ ...m, orig })).sort((a, b) => b.score - a.score);
        state.clip.review = {
          analysisId: job.analysis_id, profileId, source: data.source, live: job.live || null,
          moments: ranked.map((m, i) => ({ ...m, title: m.title || "", framing: m.framing || "cara",
            credit: m.credit || "", cam: validCam(m.cam, data.source), pick: i < count, raw: {} })),
        };
        state.clip.review.baseline = reviewSignature(state.clip.review);
      } catch (e) {
        showError(errBox, e.status === 404 ? new Error(tr("err.analysis_gone")) : e);
        return;
      }
      try {
        state.clip.keepSources = (await api("GET", "/api/settings")).keep_sources === true;
      } catch (_) { /* the note then stays visible: the default is to delete */ }
      showError(errBox, null);
      showError($("#clip-review-error"), null);
      $("#clip-status").textContent = "";
      renderClipReview();
    }
    if (userAsked) {
      setView("clips"); // the review card lives in the Clips view
      $("#clip-review-title").focus();
    }
  }

  // A stored cam box [x, y, w, h] in source pixels, or null (Baitd then finds the camera itself).
  function validCam(cam, src) {
    if (!Array.isArray(cam) || cam.length !== 4 || !cam.every(Number.isInteger)) return null;
    const [x, y, w, h] = cam;
    return x >= 0 && y >= 0 && w >= 16 && h >= 16 && x + w <= src.width && y + h <= src.height ? cam.slice() : null;
  }

  // The box clamped inside a W×H source with the band's aspect locked; `anchored` keeps x, y (resizing).
  function fitCam(W, H, x, y, w, anchored) {
    const minW = Math.max(Math.ceil(16 * CAM_ASPECT) + 1, Math.round(W * 0.06));
    let maxW = Math.min(W, H * CAM_ASPECT);
    if (anchored) maxW = Math.min(maxW, W - x, (H - y) * CAM_ASPECT);
    const width = Math.round(Math.max(Math.min(minW, maxW), Math.min(maxW, w)));
    const height = Math.min(H, Math.round(width / CAM_ASPECT));
    return [Math.round(Math.max(0, Math.min(W - width, x))), Math.round(Math.max(0, Math.min(H - height, y))), width, height];
  }

  // The Partido cam editor on a review card: a box over the preview, dragged to move and resized from its
  // corner (or with the arrow keys; Shift resizes). Untouched, m.cam stays null and Baitd finds the camera.
  function bindCamEditor(node, m, src) {
    const W = src.width, H = src.height;
    const video = $(".m-video", node);
    const box = $(".m-cam", node);
    const bar = $(".m-cam-bar", node);
    const hint = $(".m-cam-hint", node);
    const usable = src.has_video !== false && W >= 64 && H >= 64;
    const start = () => fitCam(W, H, W * 0.02, H * 0.03, W * 0.28, false); // shown until the user moves it
    let view = { scale: 1, ox: 0, oy: 0 };
    let drag = null;
    const layout = () => {
      const on = usable && m.framing === "partido";
      box.hidden = !on;
      bar.hidden = !on;
      if (!on) return;
      const vw = video.clientWidth, vh = video.clientHeight;
      const scale = Math.min(vw / W, vh / H) || 1; // the picture inside the player (object-fit: contain)
      view = { scale, ox: (vw - W * scale) / 2, oy: (vh - H * scale) / 2 };
      const [x, y, w, h] = m.cam || start();
      Object.assign(box.style, { left: `${view.ox + x * scale}px`, top: `${view.oy + y * scale}px`,
        width: `${w * scale}px`, height: `${h * scale}px` });
      box.classList.toggle("auto", !m.cam);
      hint.textContent = tr(m.cam ? "live.cam_hint_set" : "live.cam_hint_auto");
    };
    box.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      drag = { id: e.pointerId, x0: e.clientX, y0: e.clientY, box: (m.cam || start()).slice(),
        resize: e.target.classList.contains("m-cam-handle") };
      box.setPointerCapture(e.pointerId);
      box.focus();
    });
    box.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dx = (e.clientX - drag.x0) / view.scale;
      const dy = (e.clientY - drag.y0) / view.scale;
      const [x, y, w] = drag.box;
      m.cam = drag.resize
        ? fitCam(W, H, x, y, w + (Math.abs(dx) >= Math.abs(dy * CAM_ASPECT) ? dx : dy * CAM_ASPECT), true)
        : fitCam(W, H, x + dx, y + dy, w, false);
      layout();
    });
    const end = (e) => { if (drag && e.pointerId === drag.id) drag = null; };
    box.addEventListener("pointerup", end);
    box.addEventListener("pointercancel", end);
    box.addEventListener("keydown", (e) => {
      const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
      if (!d) return;
      e.preventDefault();
      const step = Math.max(2, Math.round(W / 100));
      const [x, y, w] = m.cam || start();
      m.cam = e.shiftKey ? fitCam(W, H, x, y, w + (d[0] + d[1]) * step, true)
        : fitCam(W, H, x + d[0] * step, y + d[1] * step, w, false);
      layout();
    });
    $(".m-cam-auto", node).addEventListener("click", () => { m.cam = null; layout(); });
    video.addEventListener("loadedmetadata", layout);
    if (window.ResizeObserver) new ResizeObserver(layout).observe(video);
    return layout;
  }

  function momentProblem(m, duration) {
    if (!Number.isFinite(m.start) || !Number.isFinite(m.end)) return tr("moment.err_time");
    if (m.start < 0 || m.end > duration + 0.05) return tr("moment.err_range", { d: fmtTime(duration, false) });
    const len = m.end - m.start;
    if (len < MIN_CLIP_S || len > MAX_CLIP_S) return tr("moment.err_len");
    return "";
  }

  function exportProblem() {
    const r = state.clip.review;
    const picked = r.moments.filter((m) => m.pick);
    if (!picked.length) return tr("clip.why_none");
    if (picked.length > MAX_EXPORT) return tr("clip.why_many");
    if (picked.some((m) => momentProblem(m, r.source.duration))) return tr("clip.why_bad");
    return "";
  }

  function renderExportState() {
    const r = state.clip.review;
    if (!r) return;
    const why = exportProblem();
    $("#clip-why").textContent = why;
    $("#clip-export").disabled = state.busy || Boolean(why);
    $("#clip-selected").textContent = tr("clip.selected", { n: r.moments.filter((m) => m.pick).length });
  }

  function renderClipReview() {
    const r = state.clip.review;
    if (!r) {
      $("#moment-list").replaceChildren();
      $("#clip-review").hidden = true;
      return;
    }
    $("#clip-review").hidden = false;
    $("#clip-keep-note").hidden = state.clip.keepSources === true;
    $("#clip-review-source").textContent = `${r.source.title} · ${fmtTime(r.source.duration, false)}`;
    $("#moment-list").replaceChildren(...r.moments.map((m, i) => momentCard(m, i)));
    renderExportState();
  }

  function momentCard(m, idx) {
    const r = state.clip.review;
    const node = $("#tpl-moment").content.firstElementChild.cloneNode(true);
    applyI18n(node);
    const field = (name) => {
      const el = $(`.m-${name}`, node);
      el.id = `moment-${idx}-${name}`;
      $(`.m-${name}-label`, node).htmlFor = el.id;
      return el;
    };
    const title = field("title");
    const times = { start: field("start"), end: field("end") };
    const framing = field("framing");
    const credit = field("credit");
    const pick = $(".m-pick", node);
    const remove = $(".m-remove", node);
    const named = () => {
      const name = m.title.trim() || `${fmtTime(m.start)}`;
      pick.setAttribute("aria-label", `${tr("moment.pick")}: ${name}`);
      remove.setAttribute("aria-label", `${tr("review.remove")}: ${name}`);
    };

    pick.checked = m.pick;
    pick.addEventListener("change", () => { m.pick = pick.checked; renderExportState(); });
    remove.disabled = state.busy;
    remove.addEventListener("click", () => removeMoment(m));
    // a live moment was chosen by a mark or a signal, not scored: its card says why it is there (R9)
    $(".m-score", node).textContent = r.live ? tr(`live.reason.${r.live.reason}`) : tr("moment.score", { n: m.score });
    $(".m-kind", node).textContent = tr(`clip.kind.${m.kind}`);
    title.value = m.title;
    title.addEventListener("input", () => { m.title = title.value; named(); });
    framing.value = m.framing;
    const layoutCam = bindCamEditor(node, m, r.source);
    framing.addEventListener("change", () => { m.framing = framing.value; layoutCam(); });
    credit.value = m.credit;
    credit.addEventListener("input", () => { m.credit = credit.value; });

    const text = $(".m-text", node);
    if (m.text) {
      text.textContent = m.text;
      text.hidden = false;
      const toggle = () => {
        const open = text.classList.toggle("open");
        text.setAttribute("aria-expanded", String(open));
      };
      text.addEventListener("click", toggle);
      text.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
    }

    // the preview plays only the moment: the media fragment starts it, timeupdate stops it at the end
    const video = $(".m-video", node);
    const setSrc = () => {
      if (!Number.isFinite(m.start) || !Number.isFinite(m.end) || m.end <= m.start) return;
      video.src = `/api/sources/${encodeURIComponent(r.source.id)}/media#t=${m.start},${m.end}`;
    };
    video.addEventListener("timeupdate", () => { if (video.currentTime >= m.end) video.pause(); });
    video.addEventListener("play", () => {
      $$("#moment-list .m-video").forEach((v) => { if (v !== video) v.pause(); });
      if (video.currentTime < m.start - 0.25 || video.currentTime >= m.end - 0.05) video.currentTime = m.start;
    });

    const showTimes = () => {
      for (const edge of ["start", "end"]) {
        times[edge].value = Number.isFinite(m[edge]) ? fmtTime(m[edge]) : m.raw[edge] || "";
      }
      const len = m.end - m.start;
      $(".m-len", node).textContent = Number.isFinite(len) ? tr("moment.len", { s: fmtTime(Math.max(0, len)) }) : "";
      const problem = momentProblem(m, r.source.duration);
      showError($(".m-err", node), problem ? new Error(problem) : null);
      node.classList.toggle("bad", Boolean(problem));
    };
    const changed = () => { showTimes(); setSrc(); renderExportState(); named(); };
    for (const edge of ["start", "end"]) {
      times[edge].addEventListener("change", () => {
        m.raw[edge] = times[edge].value;
        m[edge] = parseTime(times[edge].value);
        changed();
      });
    }
    $$(".m-step", node).forEach((b) => {
      const edge = b.dataset.edge;
      const delta = Number(b.dataset.delta);
      b.setAttribute("aria-label", `${tr(`moment.${edge}`)}: ${tr(delta < 0 ? "moment.minus" : "moment.plus")}`);
      b.addEventListener("click", () => {
        if (!Number.isFinite(m[edge])) return;
        m[edge] = Math.min(r.source.duration, Math.max(0, m[edge] + delta));
        changed();
      });
    });
    setSrc();
    showTimes();
    named();
    requestAnimationFrame(layoutCam); // once the card is in the page and has a size
    return node;
  }

  // Removing the last card is the same as discarding the whole analysis.
  async function removeMoment(m) {
    const r = state.clip.review;
    if (!r || state.busy) return;
    if (r.moments.length === 1) {
      await discardAnalysis();
      return;
    }
    const i = r.moments.indexOf(m);
    if (i < 0) return;
    r.moments.splice(i, 1);
    renderClipReview();
    const buttons = $$("#moment-list .m-remove");
    (buttons[Math.min(i, buttons.length - 1)] || $("#clip-discard")).focus();
  }

  async function discardAnalysis() {
    const r = state.clip.review;
    if (!r || state.busy) return;
    const yes = await ask({ title: tr("clip.discard"), text: tr("clip.discard_confirm", { title: r.source.title }),
      ok: tr("clip.discard") });
    if (!yes || state.busy) return;
    setBusy(true);
    try {
      await api("DELETE", `/api/analyses/${encodeURIComponent(r.analysisId)}`, {});
    } catch (e) {
      if (e.status !== 404) { // already gone: just close the list
        showError($("#clip-review-error"), e);
        return;
      }
    } finally {
      setBusy(false);
    }
    state.clip.review = null;
    showError($("#clip-review-error"), null);
    renderClipReview();
    $("#clip-find").focus();
  }

  async function onExport() {
    const r = state.clip.review;
    if (!r || state.busy) return;
    renderExportState();
    if (exportProblem()) return;
    showError($("#clip-review-error"), null);
    // the cam box only means something for Partido (the server refuses one it cannot place)
    const moments = r.moments.filter((m) => m.pick).map((m) => ({
      orig: m.orig, start: m.start, end: m.end, framing: m.framing, title: m.title.trim(), credit: m.credit.trim(),
      cam: m.framing === "partido" ? m.cam : null }));
    // the analysis was made with this profile; the selected one is only a fallback if it was deleted
    const profileId = profileById(r.profileId) ? r.profileId : state.current.id;
    setBusy(true);
    try {
      await flushSave(); // the export takes the hook title, credit and music switches from the saved profile
      await api("POST", `/api/analyses/${encodeURIComponent(r.analysisId)}/export`, { profile_id: profileId, moments });
      state.live.picked.delete(r.analysisId); // a live moment queued here must not go again with "Exportar seleccionados"
      state.clip.review = null;
      renderClipReview();
      $("#clip-status").textContent = tr("clip.added");
      refreshJobs();
      $("#clip-head").focus();
    } catch (e) {
      showError($("#clip-review-error"), e);
    } finally {
      setBusy(false);
    }
  }

  // ---------- queue ----------

  async function refreshJobs() {
    clearTimeout(state.pollTimer);
    try {
      const { jobs } = await api("GET", "/api/jobs");
      state.jobs = jobs;
      showError($("#queue-error"), null);
      renderJobs();
      checkAnalyses();
      onLiveJobs();
    } catch (e) {
      showError($("#queue-error"), e);
    }
    if (state.jobs.some((j) => ACTIVE.includes(j.status))) state.pollTimer = setTimeout(refreshJobs, POLL_MS);
  }

  function stepLabel(job) {
    if (job.status === "pending" || !job.step) return tr("step.waiting");
    const step = tr(`step.${job.step}`);
    const n = Math.round(job.step_percent || 0);
    // a step with its own fraction (e.g. the source download) shows it next to the name while it runs
    if (job.status === "running" && n > 0 && n < 100) return tr("progress.step_pct", { step, n });
    return step;
  }

  // "?" buttons: each shows/hides the hint it controls (aria-controls), keeping aria-expanded in sync
  function bindHelpToggles() {
    $$("button.help[aria-controls]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const panel = document.getElementById(btn.getAttribute("aria-controls"));
        if (!panel) return;
        const open = btn.getAttribute("aria-expanded") !== "true";
        btn.setAttribute("aria-expanded", String(open));
        panel.hidden = !open;
      });
    });
  }

  const isClipJob = (job) => Boolean(job.kind) && job.kind !== "short";

  function stepCount(job) {
    return tr("progress.count", { n: job.step_index, total: isClipJob(job) ? BOOT.clip_steps : BOOT.total_steps });
  }

  function renderJobs() {
    renderJobList($("#queue-list"), state.jobs.filter((j) => !isClipJob(j)), $("#queue-empty"), $("#clear-finished"));
    const clips = state.jobs.filter(isClipJob);
    // running ones first (full width), then the rest, newest first
    const sorted = [...clips.filter((j) => ACTIVE.includes(j.status)), ...clips.filter((j) => !ACTIVE.includes(j.status))];
    renderJobList($("#clip-jobs-list"), sorted, $("#clip-jobs-empty"), $("#clip-jobs-clear"));
    renderProgress();
    renderPreview();
  }

  function renderJobList(list, jobs, empty, clear) {
    const byId = new Map($$(".job", list).map((li) => [li.dataset.id, li]));
    const wanted = jobs.map((job) => {
      let li = byId.get(job.id);
      if (!li) {
        li = $("#tpl-job").content.firstElementChild.cloneNode(true);
        li.dataset.id = job.id;
        applyI18n(li);
      }
      updateJob(li, job);
      return li;
    });
    // keep existing nodes (and keyboard focus) in place; only add, remove or reorder
    byId.forEach((li, id) => { if (!jobs.some((j) => j.id === id)) li.remove(); });
    wanted.forEach((li, i) => { if (list.children[i] !== li) list.insertBefore(li, list.children[i] || null); });
    empty.hidden = jobs.length > 0;
    clear.hidden = !jobs.some((j) => FINISHED.includes(j.status));
  }

  function updateJob(li, job) {
    $(".job-topic", li).textContent = job.topic; // clip jobs: the source title, or its link until known
    const kind = $(".job-kind", li);
    kind.hidden = !isClipJob(job);
    kind.textContent = isClipJob(job) ? tr(`kind.${job.kind}`) : "";
    const chip = $(".chip", li);
    chip.textContent = tr(`status.${job.status}`);
    chip.dataset.status = job.status;
    const running = job.status === "running";
    $(".job-step", li).hidden = !ACTIVE.includes(job.status);
    $(".job-label", li).textContent = stepLabel(job);
    $(".job-count", li).textContent = running && job.step_index ? stepCount(job) : "";
    const bar = $(".bar", li);
    bar.hidden = !running;
    $("i", bar).style.width = `${job.percent}%`;
    showError($(".job-err", li), job.error ? new Error(job.error) : null);
    // a finished analysis has no folder: it opens its review list instead
    $('[data-job-act="review"]', li).hidden = !(job.kind === "clip_analyze" && job.status === "done" && job.analysis_id);
    $('[data-job-act="clips"]', li).hidden = !(job.status === "done" && job.output);
    $('[data-job-act="open"]', li).hidden = !(job.status === "done" && job.output);
    $('[data-job-act="retry"]', li).hidden = !RETRYABLE.includes(job.status);
    $('[data-job-act="cancel"]', li).hidden = !ACTIVE.includes(job.status);
    $('[data-job-act="remove"]', li).hidden = !FINISHED.includes(job.status);
  }

  function renderProgress() {
    const shorts = state.jobs.filter((j) => !isClipJob(j));
    const job = shorts.find((j) => j.status === "running") || shorts.find((j) => j.status === "pending");
    const box = $("#progress");
    box.hidden = !job;
    if (!job) return;
    const pct = job.status === "running" ? job.percent : 0;
    $("#progress-fill").style.width = `${pct}%`;
    $("#progress .bar").setAttribute("aria-valuenow", String(Math.round(pct)));
    $("#progress-step").textContent = `${stepLabel(job)}`;
    $("#progress-count").textContent = job.step_index ? stepCount(job) : "";
  }

  function renderPreview() {
    const latest = state.jobs.filter((j) => !isClipJob(j)).reverse().find((j) => j.status === "done" && j.has_video);
    const video = $("#preview");
    const id = latest ? latest.id : null;
    if (id === state.previewId) return;
    state.previewId = id;
    if (id) {
      video.src = `/api/jobs/${encodeURIComponent(id)}/video`;
      video.hidden = false;
      $("#sample-sub").hidden = true;
    } else {
      video.removeAttribute("src");
      video.load();
      video.hidden = true;
      $("#sample-sub").hidden = false;
    }
  }

  // The finished videos of a job, playable inside the app.
  let clipsSeq = 0; // two quick "Ver videos" clicks (or a close meanwhile): only the latest call adds tiles

  async function openClips(id) {
    const seq = ++clipsSeq;
    const dlg = $("#clips-view");
    const grid = $("#clips-grid");
    grid.replaceChildren();
    $("#clips-empty").hidden = true;
    showError($("#clips-error"), null);
    dlg.dataset.job = id;
    try {
      const r = await api("GET", `/api/jobs/${encodeURIComponent(id)}/clips`);
      if (seq !== clipsSeq) return;
      grid.classList.toggle("wide", r.aspect === "16:9");
      grid.style.setProperty("--clip-ar", r.aspect.replace(":", " / "));
      for (const c of r.clips) {
        const tile = document.createElement("figure");
        tile.className = "clip-tile";
        const video = document.createElement("video");
        video.controls = true;
        video.preload = "metadata";
        video.src = `/api/jobs/${encodeURIComponent(id)}/files/${encodeURIComponent(c.file)}`;
        const cap = document.createElement("figcaption");
        cap.textContent = c.title || c.file;
        tile.append(video, cap);
        grid.append(tile);
      }
      $("#clips-empty").hidden = r.clips.length > 0;
    } catch (err) {
      if (seq !== clipsSeq) return;
      showError($("#clips-error"), err);
    }
    if (!dlg.open) dlg.showModal();
  }

  function unloadClips() { // also what Esc does: the videos stop playing and downloading
    clipsSeq++; // a reply still on its way must not reopen or fill the dialog
    $$("#clips-view video").forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
    $("#clips-grid").replaceChildren();
  }

  function closeClips() {
    unloadClips();
    $("#clips-view").close();
  }

  async function onJobAction(e) {
    const btn = e.target.closest("[data-job-act]");
    if (!btn) return;
    const id = btn.closest(".job").dataset.id;
    const act = btn.dataset.jobAct;
    const inClips = Boolean(btn.closest("#clip-jobs"));
    const errBox = $(inClips ? "#clip-jobs-error" : "#queue-error");
    if (act === "review") {
      const job = state.jobs.find((j) => j.id === id);
      if (job) await openReview(job, true);
      return;
    }
    if (act === "clips") {
      await openClips(id);
      return;
    }
    const url = {
      open: [`/api/jobs/${encodeURIComponent(id)}/open-folder`, "POST"],
      cancel: [`/api/jobs/${encodeURIComponent(id)}/cancel`, "POST"],
      retry: [`/api/jobs/${encodeURIComponent(id)}/retry`, "POST"],
      remove: [`/api/jobs/${encodeURIComponent(id)}`, "DELETE"],
    }[act];
    try {
      await api(url[1], url[0], {});
      showError(errBox, null);
    } catch (err) {
      showError(errBox, err);
    }
    if (act !== "open") {
      await refreshJobs();
      if (!document.contains(btn) || btn.hidden) $(inClips ? "#clip-jobs-title" : "#queue-title").focus(); // keep keyboard focus in its list
    }
  }

  // ---------- en directo: panel lifetime ----------

  // The panel is "shown" while the Clipping tab, its En directo sub-tab and the window are all visible:
  // opening it re-checks the channels and reloads the moments; the channel list refreshes every minute.
  function syncLiveShown() {
    const L = state.live;
    const shown = L.ready && state.tab === "clip" && L.sub === "live" && document.visibilityState === "visible";
    if (shown === L.shown) return;
    L.shown = shown;
    clearTimeout(L.channelsTimer);
    if (!shown) return;
    loadChannels(true);
    loadMoments();
  }

  function scheduleChannels(ms) {
    const L = state.live;
    clearTimeout(L.channelsTimer);
    if (L.shown) L.channelsTimer = setTimeout(() => loadChannels(false), ms);
  }

  // ---------- en directo: channels ----------

  const SVG_NS = "http://www.w3.org/2000/svg";
  // simple marks that only identify the platform: [background, glyph, glyph colour]
  const LOGOS = {
    kick: ["#53FC18", "M6 4h4v5h1V7h1V5h1V4h4v5h-1v1h-1v1h-1v2h1v1h1v1h1v5h-4v-1h-1v-2h-1v-2h-1v5H6z", "#000000"],
    twitch: ["#9146FF", "M6 4 5 7v11h3v2h2l2-2h3l4-4V4zm12 9.3L15.8 15.5h-3.6L10 17.5v-2H7.2V5.6H18zM11 8h1.6v4H11zm4 0h1.6v4H15z",
      "#ffffff"],
    youtube: ["#FF0000", "M9.5 7.5v9l7.5-4.5z", "#ffffff"],
  };

  function platformLogo(platform) {
    const [bg, glyph, fg] = LOGOS[platform] || ["#888888", "", "#ffffff"];
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "logo");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", tr(`live.platform.${platform}`));
    const rect = document.createElementNS(SVG_NS, "rect");
    Object.entries({ width: 24, height: 24, rx: 6, fill: bg }).forEach(([k, v]) => rect.setAttribute(k, String(v)));
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", glyph);
    path.setAttribute("fill", fg);
    path.setAttribute("fill-rule", "evenodd");
    svg.append(rect, path);
    return svg;
  }

  // "@handle"; a YouTube channel id (UC…) shows the channel's name instead when it is known.
  function channelLabel(platform, handle, name) {
    if (platform === "youtube" && /^UC[\w-]{22}$/.test(handle)) return name || handle;
    return `@${String(handle).replace(/^@/, "")}`;
  }

  const liveActive = () => Boolean(state.live.status) && LIVE_ACTIVE.includes(state.live.status.state);
  const capturingId = () => (liveActive() ? state.live.status.channel_id : null);

  async function loadChannels(recheck) {
    const L = state.live;
    try {
      const { channels } = recheck ? await api("POST", "/api/live/channels/refresh", {}) : await api("GET", "/api/live/channels");
      L.channels = channels;
      showError($("#live-channels-error"), null);
    } catch (e) {
      showError($("#live-channels-error"), e);
    }
    renderChannels();
    scheduleChannels(recheck ? CHANNELS_RECHECK_MS : CHANNELS_MS);
  }

  function renderChannels() {
    const L = state.live;
    if (!L.ready) return;
    const list = $("#live-channels");
    const byId = new Map($$(".channel", list).map((li) => [li.dataset.id, li]));
    const wanted = L.channels.map((ch) => {
      let li = byId.get(ch.id);
      if (!li) li = channelRow(ch.id);
      updateChannel(li, ch);
      return li;
    });
    byId.forEach((li, id) => { if (!L.channels.some((c) => c.id === id)) li.remove(); });
    wanted.forEach((li, i) => { if (list.children[i] !== li) list.insertBefore(li, list.children[i] || null); });
    $("#live-channels-empty").hidden = L.channels.length > 0;
  }

  // One row, wired once: its handlers look the channel up by id, so a refreshed list keeps them working.
  function channelRow(id) {
    const li = $("#tpl-channel").content.firstElementChild.cloneNode(true);
    li.dataset.id = id;
    applyI18n(li);
    const ch = () => state.live.channels.find((c) => c.id === id);
    const mine = $(".ch-mine", li);
    mine.addEventListener("change", () => patchChannel(li, { mine: mine.checked }));
    const framing = $(".ch-framing", li);
    framing.addEventListener("change", () => patchChannel(li, { framing: framing.value }));
    const delay = $(".ch-delay", li);
    delay.addEventListener("change", () => {
      const v = delay.value.trim().replace(",", ".");
      patchChannel(li, { delay_s: v === "" ? null : Number(v) });
    });
    $(".ch-capture", li).addEventListener("click", () => { if (ch()) startCapture(li, id); });
    $(".ch-remove", li).addEventListener("click", () => { if (ch()) removeChannel(li, id); });
    return li;
  }

  function updateChannel(li, ch) {
    const label = channelLabel(ch.platform, ch.handle, ch.name);
    const logo = $(".ch-logo", li);
    if (logo.dataset.platform !== ch.platform || logo.dataset.lang !== BOOT.lang) {
      logo.replaceChildren(platformLogo(ch.platform));
      logo.dataset.platform = ch.platform;
      logo.dataset.lang = BOOT.lang;
    }
    $(".ch-handle", li).textContent = label;
    const known = ch.live === true || ch.live === false;
    $(".ch-dot", li).dataset.live = known ? String(ch.live) : "unknown";
    $(".ch-live", li).textContent = tr(!known ? "live.checking" : ch.live ? "live.on_air" : "live.off_air");
    const viewers = ch.live && Number.isFinite(Number(ch.viewers)) && ch.viewers !== null;
    $(".ch-viewers", li).textContent = viewers ? tr("live.viewers", { n: Number(ch.viewers).toLocaleString(BOOT.lang) }) : "";
    const title = $(".ch-title", li);
    title.textContent = ch.live && ch.title ? ch.title : "";
    title.hidden = !title.textContent;
    const mine = $(".ch-mine", li);
    mine.checked = ch.mine;
    mine.setAttribute("aria-label", `${tr("live.mine")}: ${label}`);
    const framing = $(".ch-framing", li);
    framing.value = ch.framing;
    framing.setAttribute("aria-label", `${tr("live.framing")}: ${label}`);
    // the delay is only used when the stream has no clock of its own (R5): shown once a capture found none
    const delay = $(".ch-delay", li);
    $(".ch-delay-wrap", li).hidden = !(state.live.noPdt.has(ch.id) || ch.delay_s !== null);
    if (document.activeElement !== delay) delay.value = ch.delay_s === null ? "" : String(ch.delay_s);
    const busyId = capturingId();
    const capture = $(".ch-capture", li);
    capture.textContent = tr(busyId === ch.id ? "live.capturing" : "live.capture");
    capture.disabled = busyId !== null; // one capture at a time
    capture.setAttribute("aria-label", `${capture.textContent}: ${label}`);
    const remove = $(".ch-remove", li);
    remove.disabled = busyId === ch.id;
    remove.setAttribute("aria-label", `${tr("live.remove")}: ${label}`);
  }

  async function patchChannel(li, body) {
    const id = li.dataset.id;
    try {
      const row = await api("PATCH", `/api/live/channels/${encodeURIComponent(id)}`, body);
      const i = state.live.channels.findIndex((c) => c.id === id);
      if (i >= 0) state.live.channels[i] = row;
      showError($(".ch-err", li), null);
    } catch (e) {
      showError($(".ch-err", li), e); // the row goes back to what the server has
    }
    renderChannels();
  }

  async function addChannel() {
    const input = $("#live-url");
    const btn = $("#live-add");
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const row = await api("POST", "/api/live/channels", { url: input.value, mine: $("#live-mine").checked });
      if (!state.live.channels.some((c) => c.id === row.id)) state.live.channels.push(row);
      input.value = "";
      $("#live-mine").checked = false;
      showError($("#live-channels-error"), null);
      renderChannels();
      scheduleChannels(CHANNELS_RECHECK_MS); // its live status is being checked now
    } catch (e) {
      showError($("#live-channels-error"), e);
    } finally {
      btn.disabled = false;
    }
  }

  async function removeChannel(li, id) {
    try {
      await api("DELETE", `/api/live/channels/${encodeURIComponent(id)}`, {});
      state.live.channels = state.live.channels.filter((c) => c.id !== id);
      state.live.noPdt.delete(id);
      renderChannels();
      $("#live-url").focus();
    } catch (e) {
      if (e.status === 404) { loadChannels(false); return; }
      showError($(".ch-err", li), e);
    }
  }

  // ---------- en directo: capture ----------

  async function startCapture(li, id) {
    showError($(".ch-err", li), null);
    showError($("#live-capture-error"), null);
    try {
      state.live.status = await api("POST", "/api/live/capture", { channel_id: id });
    } catch (e) {
      showError($(".ch-err", li), e);
      return;
    }
    showAppAlert(""); // "no capture" no longer applies
    renderCapture();
    renderChannels();
    pollStatus();
    $("#live-mark").focus();
  }

  async function stopCapture() {
    const btn = $("#live-stop");
    btn.disabled = true;
    try {
      await api("DELETE", "/api/live/capture", {}); // returns at once: the state changes in the next polls
      showError($("#live-capture-error"), null);
    } catch (e) {
      showError($("#live-capture-error"), e);
    } finally {
      btn.disabled = false;
    }
    pollStatus();
  }

  async function markMoment() {
    try {
      await api("POST", "/api/live/mark", {}); // its event (marked, extended…) arrives with the status
      showError($("#live-capture-error"), null);
    } catch (e) {
      showError($("#live-capture-error"), e);
    }
    pollStatus();
  }

  // GET /api/live/status every second while capturing, every 10 s otherwise. Events become notices; a
  // saved moment reloads the list. Events from before the page opened are not shown.
  let statusInFlight = false;

  async function pollStatus() {
    const L = state.live;
    clearTimeout(L.statusTimer);
    if (statusInFlight) { // the running request schedules the next one
      L.statusTimer = setTimeout(pollStatus, LIVE_FAST_MS);
      return;
    }
    statusInFlight = true;
    const wasActive = liveActive();
    const wasId = capturingId();
    try {
      const st = await api("GET", `/api/live/status?since=${L.since === null ? 0 : L.since}`);
      const first = L.since === null;
      let since = first ? 0 : L.since;
      let reload = false;
      for (const ev of st.events || []) {
        if (ev.id <= since) continue;
        since = ev.id;
        if (first) continue;
        addNotice(ev);
        if (ev.kind === "saved" || ev.kind === "found") reload = true;
      }
      L.since = since;
      L.status = st;
      syncHotkeyAlert(st.hotkey);
      if (st.channel_id && st.state === "capturing") {
        if (st.has_pdt === false) L.noPdt.add(st.channel_id);
        else if (st.has_pdt === true) L.noPdt.delete(st.channel_id);
      }
      showError($("#live-capture-error"), null);
      if (reload) loadMoments();
    } catch (e) {
      showError($("#live-capture-error"), e);
    } finally {
      statusInFlight = false;
    }
    renderCapture();
    if (liveActive() !== wasActive || capturingId() !== wasId || liveActive()) renderChannels();
    L.statusTimer = setTimeout(pollStatus, liveActive() ? LIVE_FAST_MS : LIVE_IDLE_MS);
  }

  function renderCapture() {
    const L = state.live;
    if (!L.ready) return;
    const st = L.status;
    const box = $("#live-capture");
    box.hidden = !liveActive();
    if (box.hidden) return;
    const ch = L.channels.find((c) => c.id === st.channel_id);
    const logo = $(".cap-logo", box);
    const platform = ch ? ch.platform : "";
    if (logo.dataset.platform !== platform || logo.dataset.lang !== BOOT.lang) {
      logo.replaceChildren(...(ch ? [platformLogo(ch.platform)] : []));
      logo.dataset.platform = platform;
      logo.dataset.lang = BOOT.lang;
    }
    $("#live-cap-handle").textContent = ch ? channelLabel(ch.platform, ch.handle, ch.name) : "";
    const chip = $("#live-cap-state");
    chip.textContent = tr(`live.state.${st.state}`);
    chip.dataset.status = { starting: "pending", capturing: "running", reconnecting: "error" }[st.state];
    $("#live-cap-title").textContent = st.title || "";
    $("#live-cap-title").hidden = !st.title;
    $("#live-cap-latency").textContent = st.latency_s === null ? "" : tr("live.latency", { s: Math.round(st.latency_s) });
    $("#live-cap-buffered").textContent = tr("live.buffered", {
      a: fmtTime(Math.min(BUFFER_S, st.buffered_s || 0), false), b: fmtTime(BUFFER_S, false) });
    $("#live-cap-ai").textContent = st.ai_left === null || st.ai_left === undefined ? "" : tr("live.ai_left", { n: st.ai_left });
    $("#live-pending").textContent = st.pending ? tr("live.pending", { s: Math.ceil(st.pending.remaining_s) }) : "";
    const hk = st.hotkey;
    $("#live-hotkey-hint").textContent = hk && !hk.ok
      ? tr("live.hotkey_off", { key: hk.spec })
      : tr("live.hotkey_tip", { key: L.hotkey });
  }

  // A hotkey press with no capture, or a hotkey the system refused, is explained on every tab (spec §6.1):
  // in the app-wide line under the header instead of the panel's notices.
  let appAlertTimer = null;
  let appAlertNow = ""; // the passing notice (a press without a capture, a refusal as it happens)
  let hotkeyAlert = ""; // the standing one: the system refused the hotkey, so it stays until it works

  function paintAppAlert() {
    const el = $("#live-alert");
    el.textContent = appAlertNow || hotkeyAlert;
    el.hidden = !el.textContent;
  }

  function showAppAlert(text) {
    clearTimeout(appAlertTimer);
    appAlertNow = text;
    paintAppAlert();
    if (text) appAlertTimer = setTimeout(() => showAppAlert(""), NOTICE_WARN_MS);
  }

  // GET /api/live/status says whether the hotkey is registered, also for a refusal at start-up, before the
  // first poll (events from before the page opened are not shown).
  function syncHotkeyAlert(hk) {
    const text = hk && !hk.ok ? hk.text : "";
    if (text === hotkeyAlert) return;
    hotkeyAlert = text;
    paintAppAlert();
  }

  function addNotice(ev) {
    const L = state.live;
    if (APP_NOTICES.includes(ev.kind)) { showAppAlert(ev.text); return; }
    const warn = NOTICE_WARN.includes(ev.kind);
    L.notices.push({ id: ev.id, kind: ev.kind, text: ev.text, warn, until: Date.now() + (warn ? NOTICE_WARN_MS : NOTICE_MS) });
    L.notices = L.notices.slice(-4);
    renderNotices();
    setTimeout(renderNotices, (warn ? NOTICE_WARN_MS : NOTICE_MS) + 50);
  }

  // The server writes the event texts (already user copy, in Spanish); they are shown as they come.
  function renderNotices() {
    const L = state.live;
    const now = Date.now();
    L.notices = L.notices.filter((n) => n.until > now);
    const list = $("#live-notices");
    const byId = new Map($$("li", list).map((li) => [li.dataset.id, li]));
    const items = L.notices.map((n) => {
      let li = byId.get(String(n.id));
      if (!li) {
        li = document.createElement("li");
        li.dataset.id = String(n.id);
        li.className = n.warn ? "notice warn" : "notice";
        li.textContent = n.text;
      }
      return li;
    });
    list.replaceChildren(...items);
    list.hidden = !items.length;
  }

  // ---------- en directo: moments ----------

  async function loadMoments() {
    const L = state.live;
    const seq = ++L.momentsSeq;
    try {
      const { moments } = await api("GET", "/api/live/moments");
      if (seq !== L.momentsSeq) return;
      L.moments = moments;
      if (L.momentsLoadFailed) showError($("#live-moments-error"), null);
      L.momentsLoadFailed = false;
    } catch (e) {
      if (seq !== L.momentsSeq) return;
      L.momentsLoadFailed = true;
      showError($("#live-moments-error"), e);
      return;
    }
    const ids = new Set(L.moments.map((m) => m.analysis_id));
    L.picked = new Set([...L.picked].filter((id) => ids.has(id)));
    L.cardErrors.forEach((_, id) => { if (!ids.has(id)) L.cardErrors.delete(id); });
    renderMoments();
  }

  // A clip_export or live_title job on that moment still in the queue.
  const momentJob = (id, kind) => state.jobs.find((j) => j.kind === kind && j.analysis_id === id && ACTIVE.includes(j.status));

  // A moment leaves the list once its export succeeds and gets its AI title when that job ends: both
  // reload the list when such a job finishes.
  function onLiveJobs() {
    const L = state.live;
    const now = new Map(state.jobs.map((j) => [j.id, j]));
    let reload = false;
    L.jobStatus.forEach((status, id) => {
      const j = now.get(id);
      if (ACTIVE.includes(status) && (!j || FINISHED.includes(j.status))) reload = true;
    });
    L.jobStatus = new Map(state.jobs.filter((j) => j.kind === "clip_export" || j.kind === "live_title")
      .map((j) => [j.id, j.status]));
    if (reload) loadMoments();
    else renderMoments();
  }

  function fmtClock(iso) {
    const d = new Date(iso); // local time without an offset: read as local
    if (Number.isNaN(d.getTime())) return "";
    const time = d.toLocaleTimeString(BOOT.lang, { hour: "2-digit", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString()
      ? time : `${d.toLocaleDateString(BOOT.lang, { day: "numeric", month: "short" })} ${time}`;
  }

  function renderMoments() {
    const L = state.live;
    if (!L.ready) return;
    const list = $("#live-moments");
    const byId = new Map($$(".lm", list).map((n) => [n.dataset.id, n]));
    const wanted = L.moments.map((m) => {
      let node = byId.get(m.analysis_id);
      if (!node) node = liveMomentCard(m.analysis_id);
      updateLiveMoment(node, m);
      return node;
    });
    byId.forEach((n, id) => { if (!L.moments.some((m) => m.analysis_id === id)) n.remove(); });
    wanted.forEach((n, i) => { if (list.children[i] !== n) list.insertBefore(n, list.children[i] || null); });
    $("#live-moments-empty").hidden = L.moments.length > 0;
    $("#live-selected").textContent = L.moments.length ? tr("clip.selected", { n: L.picked.size }) : "";
    $("#live-export").disabled = L.exporting || !L.picked.size;
  }

  function liveMomentCard(id) {
    const node = $("#tpl-live-moment").content.firstElementChild.cloneNode(true);
    node.dataset.id = id;
    applyI18n(node);
    const entry = () => state.live.moments.find((m) => m.analysis_id === id);
    const pick = $(".lm-pick", node);
    pick.addEventListener("change", () => {
      if (pick.checked) state.live.picked.add(id); else state.live.picked.delete(id);
      renderMoments();
    });
    $(".lm-review", node).addEventListener("click", () => {
      const m = entry();
      if (m) openReview({ analysis_id: id, live: m }, true);
    });
    $(".lm-ai", node).addEventListener("click", () => askTitle(id));
    $(".lm-remove", node).addEventListener("click", () => { const m = entry(); if (m) removeLiveMoment(m); });
    return node;
  }

  function updateLiveMoment(node, m) {
    const L = state.live;
    const id = m.analysis_id;
    const img = $(".lm-thumb", node);
    if (m.has_thumb && !img.getAttribute("src")) img.src = `/api/live/moments/${encodeURIComponent(id)}/thumb`;
    img.hidden = !m.has_thumb;
    $(".lm-pic", node).classList.toggle("empty", !m.has_thumb);
    $(".lm-title", node).textContent = m.title;
    const logo = $(".lm-logo", node);
    if (logo.dataset.platform !== m.platform || logo.dataset.lang !== BOOT.lang) {
      logo.replaceChildren(platformLogo(m.platform));
      logo.dataset.platform = m.platform;
      logo.dataset.lang = BOOT.lang;
    }
    $(".lm-handle", node).textContent = channelLabel(m.platform, m.handle, "");
    $(".lm-reason", node).textContent = tr(`live.reason.${m.reason}`);
    $(".lm-time", node).textContent = fmtClock(m.at);
    // the marked moment, not the file (which carries up to 20 s of review padding on each side)
    $(".lm-dur", node).textContent = tr("moment.len", { s: fmtTime(m.length, false) });
    $(".lm-gap", node).hidden = !m.gap;
    $(".lm-unclassified", node).hidden = m.reason === "manual" || m.classified;
    const exporting = Boolean(momentJob(id, "clip_export"));
    if (exporting) L.picked.delete(id); // already queued: the count and the export button follow
    const asking = L.aiPosting.has(id) || Boolean(momentJob(id, "live_title"));
    const busy = $(".lm-busy", node);
    busy.textContent = exporting ? tr("live.exporting") : asking ? tr("live.ai_busy") : "";
    busy.hidden = !busy.textContent;
    const pick = $(".lm-pick", node);
    pick.checked = L.picked.has(id);
    pick.disabled = exporting || L.exporting;
    pick.setAttribute("aria-label", `${tr("moment.pick")}: ${m.title}`);
    const ai = $(".lm-ai", node);
    ai.disabled = asking || exporting;
    ai.setAttribute("aria-label", `${tr("live.ai_title")}: ${m.title}`);
    $(".lm-review", node).setAttribute("aria-label", `${tr("live.review")}: ${m.title}`);
    const remove = $(".lm-remove", node);
    remove.disabled = exporting || L.exporting;
    remove.setAttribute("aria-label", `${tr("live.remove")}: ${m.title}`);
    const err = L.cardErrors.get(id);
    showError($(".lm-err", node), err ? new Error(err) : null);
  }

  async function askTitle(id) {
    const L = state.live;
    if (L.aiPosting.has(id) || momentJob(id, "live_title")) return;
    L.aiPosting.add(id);
    L.cardErrors.delete(id);
    renderMoments();
    try {
      await flushSave().catch(() => {}); // the job takes the AI tone from the saved profile
      await api("POST", `/api/live/moments/${encodeURIComponent(id)}/ai`, { profile_id: state.current.id });
      await refreshJobs(); // the queue shows it; its end reloads the list
    } catch (e) {
      L.cardErrors.set(id, e.status === 404 ? tr("err.analysis_gone") : e.message);
    } finally {
      L.aiPosting.delete(id);
      renderMoments();
    }
  }

  async function removeLiveMoment(m) {
    const yes = await ask({ title: tr("live.remove_title"), text: tr("live.remove_confirm", { title: m.title }),
      ok: tr("live.remove") });
    if (!yes) return;
    const id = m.analysis_id;
    try {
      await api("DELETE", `/api/analyses/${encodeURIComponent(id)}`, {});
    } catch (e) {
      if (e.status !== 404) { // already gone: the reload drops it
        state.live.cardErrors.set(id, e.message);
        renderMoments();
        return;
      }
    }
    if (state.clip.review && state.clip.review.analysisId === id) { // its open review card has nothing left
      state.clip.review = null;
      renderClipReview();
    }
    state.live.picked.delete(id);
    await loadMoments();
    $("#live-moments-title").focus();
  }

  // One export per ticked moment, each with the moment as stored (title, framing, credit, cam box).
  async function exportLiveMoments() {
    const L = state.live;
    // one already in the queue (e.g. exported from its review card) is not queued a second time
    const ids = L.moments.map((m) => m.analysis_id).filter((id) => L.picked.has(id) && !momentJob(id, "clip_export"));
    if (!ids.length || L.exporting || !state.current) return;
    L.exporting = true;
    $("#live-moments-status").textContent = "";
    showError($("#live-moments-error"), null);
    renderMoments();
    let added = 0;
    try {
      await flushSave().catch(() => {}); // the export takes hook title, credit and music from the saved profile
      for (const id of ids) {
        try {
          const data = await api("GET", `/api/analyses/${encodeURIComponent(id)}`);
          const m = data.moments[0];
          const cam = m.framing === "partido" ? validCam(m.cam, data.source) : null;
          await api("POST", `/api/analyses/${encodeURIComponent(id)}/export`, { profile_id: state.current.id,
            moments: [{ orig: 0, start: m.start, end: m.end, framing: m.framing || "cara", title: m.title || "",
              credit: m.credit || "", cam }] });
          added++;
          L.picked.delete(id);
          L.cardErrors.delete(id);
        } catch (e) {
          L.cardErrors.set(id, e.status === 404 ? tr("err.analysis_gone") : e.message);
        }
      }
    } finally {
      L.exporting = false;
    }
    if (added) $("#live-moments-status").textContent = tr("live.export_added", { n: added });
    await refreshJobs();
    renderMoments();
  }

  // ---------- en directo: the hotkey recorder in Ajustes ----------

  const HOTKEY_NAMED = { Pause: "Pause", Insert: "Insert", Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown" };

  // The pressed combination in the server's canonical form ("Ctrl+Shift+F9"), or {error: i18n key}.
  function hotkeyFromEvent(e) {
    const code = e.code || "";
    let key = null;
    if (/^F([1-9]|1\d|2[0-4])$/.test(code)) key = code;
    else if (/^Key[A-Z]$/.test(code)) key = code.slice(3);
    else if (/^Digit\d$/.test(code)) key = code.slice(5);
    else if (HOTKEY_NAMED[code]) key = HOTKEY_NAMED[code];
    if (!key) return { error: "live.hotkey_bad" };
    const mods = [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Win"].filter(Boolean);
    // a letter or digit alone, or with Shift only (capitals), would steal typing: it needs Ctrl, Alt or Win
    if (key.length === 1 && !(e.ctrlKey || e.altKey || e.metaKey)) return { error: "live.hotkey_needs_mod" };
    return { value: [...mods, key].join("+") };
  }

  function setHotkeyHint(key) {
    const hint = $("#hotkey-hint");
    hint.dataset.i18n = key;
    hint.textContent = tr(key);
  }

  function startHotkeyRecord() {
    const btn = $("#s-hotkey-record");
    state.live.rec = { before: $("#s-live_hotkey").value };
    btn.setAttribute("aria-pressed", "true");
    btn.dataset.i18n = "live.hotkey_recording";
    btn.textContent = tr("live.hotkey_recording");
    setHotkeyHint("live.hotkey_recording_hint");
    btn.focus();
  }

  // kept: the new combination stays in the field (saved with Guardar); otherwise the old one comes back.
  function stopHotkeyRecord(kept) {
    const rec = state.live.rec;
    if (!rec) return;
    state.live.rec = null;
    if (!kept) $("#s-live_hotkey").value = rec.before;
    const btn = $("#s-hotkey-record");
    btn.setAttribute("aria-pressed", "false");
    btn.dataset.i18n = "live.hotkey_change";
    btn.textContent = tr("live.hotkey_change");
    setHotkeyHint("live.hotkey_hint");
  }

  // While recording, the next key down is the new hotkey. Esc cancels (and must not close the dialog);
  // Tab leaves as usual; modifiers alone wait for the key.
  function onHotkeyKey(e) {
    if (!state.live.rec) return;
    if (["Control", "Shift", "Alt", "Meta", "OS"].includes(e.key)) return;
    if (e.key === "Tab") { stopHotkeyRecord(false); return; }
    e.preventDefault();
    e.stopPropagation();
    if (e.key === "Escape") {
      setTimeout(() => stopHotkeyRecord(false), 0); // still recording when the dialog's cancel event comes
      return;
    }
    const got = hotkeyFromEvent(e);
    if (got.error) { setHotkeyHint(got.error); return; }
    $("#s-live_hotkey").value = got.value;
    stopHotkeyRecord(true);
  }

  async function loadHotkey() {
    try {
      const s = await api("GET", "/api/settings");
      if (s.live_hotkey) state.live.hotkey = s.live_hotkey;
    } catch (_) { /* the tip then shows the default key */ }
    renderCapture();
  }

  // ---------- settings dialog ----------

  async function openSettings() {
    const dlg = $("#settings");
    showError($("#settings-error"), null);
    try {
      const s = await api("GET", "/api/settings");
      $$("[data-setting]", dlg).forEach((el) => { // numbers too: 0 IA requests per hour is a value
        const v = s[el.dataset.setting];
        el.value = v === undefined || v === null ? "" : String(v);
      });
      stopHotkeyRecord(false);
      loadModels(); // fills the model list for the saved provider; does not block the dialog
      $("#s-keep_sources").checked = s.keep_sources === true;
      $("#s-check_updates").checked = s.check_updates !== false;
      $("#ytdlp-status").textContent = "";
      showError($("#ytdlp-error"), null);
      $$(".show-btn", dlg).forEach((b) => setShown(b, false));
    } catch (e) {
      showError($("#top-error"), e);
      return;
    }
    dlg.showModal();
  }

  // AI model picker: "Por defecto ({model})", the models the saved key can use, "Otro…" (free text).
  const MODEL_OTHER = "__other__";
  let modelsSeq = 0; // a late reply for a provider that is no longer selected is dropped

  // Fills the select from `models` and selects `current` ("" = default, unlisted = Otro…).
  function fillModelPicker(models, def, current) {
    const pick = $("#s-llm_model_pick");
    const opt = (value, text) => Object.assign(document.createElement("option"), { value, textContent: text });
    pick.replaceChildren(
      opt("", def ? tr("ai.model_default", { model: def }) : tr("ai.model_default_plain")),
      ...models.map((m) => opt(m, m)),
      opt(MODEL_OTHER, tr("ai.model_other")),
    );
    pick.value = current === "" ? "" : (models.includes(current) ? current : MODEL_OTHER);
    syncModelInput(false);
  }

  // Keeps the text input (the saved setting) in step with the select; it shows only for "Otro…".
  function syncModelInput(fromUser) {
    const pick = $("#s-llm_model_pick").value;
    const input = $("#s-llm_model");
    if (pick !== MODEL_OTHER) input.value = pick;
    input.hidden = pick !== MODEL_OTHER;
    input.closest(".field").hidden = input.hidden; // the label goes with the input
    if (fromUser && pick === MODEL_OTHER) input.focus();
  }

  function setModelHint(message) {
    const hint = $("#llm-hint");
    if (message) {
      hint.removeAttribute("data-i18n");
      hint.textContent = message;
    } else {
      hint.dataset.i18n = "ai.hint";
      hint.textContent = tr("ai.hint");
    }
  }

  async function loadModels() {
    const seq = ++modelsSeq;
    const provider = $("#s-llm_provider").value;
    const current = $("#s-llm_model").value.trim();
    const btn = $("#llm-models-refresh");
    btn.disabled = true;
    let models = [], def = "", message = "";
    try {
      const r = await api("GET", `/api/llm/models?provider=${encodeURIComponent(provider)}`);
      models = r.models;
      def = r.default;
    } catch (e) {
      message = e.message; // no key yet, no connection...: "Por defecto" and "Otro…" still work
    }
    if (seq !== modelsSeq) return;
    btn.disabled = false;
    setModelHint(message);
    fillModelPicker(models, def, current);
  }

  function setShown(btn, shown) {
    const input = document.getElementById(btn.getAttribute("aria-controls"));
    input.type = shown ? "text" : "password";
    btn.setAttribute("aria-pressed", String(shown));
    btn.dataset.i18n = shown ? "key.hide" : "key.show";
    btn.textContent = tr(btn.dataset.i18n);
  }

  async function quitApp() {
    const yes = await ask({ title: tr("settings.quit"), text: tr("settings.quit_text"), ok: tr("settings.quit") });
    if (!yes) return;
    try {
      await api("POST", "/api/shutdown", {});
    } catch (e) {
      showError($("#settings-error"), e);
      return;
    }
    $("#settings").close();
    document.body.textContent = tr("settings.quit_done");
    document.body.className = "quit-done";
  }

  // ---------- update notice ----------

  const updateInfo = { latest: null, canInstall: false };

  function renderUpdateBar() {
    const bar = $("#update-bar");
    const v = updateInfo.latest && updateInfo.latest.version;
    if (!v || remembered("updateDismissed") === v) { bar.hidden = true; return; }
    $("#update-text").textContent = tr("update.available", { v });
    $("#update-install").hidden = !updateInfo.canInstall;
    $("#update-download").hidden = updateInfo.canInstall;
    bar.hidden = false;
  }

  async function loadUpdate() {
    try {
      const u = await api("GET", "/api/update");
      updateInfo.latest = u.latest || null;
      updateInfo.canInstall = u.can_install === true;
      renderUpdateBar();
      return Boolean(updateInfo.latest);
    } catch (_) { return false; } // the notice is optional
  }

  async function pollUpdate() { // the background check may still be running at startup: ask at 0 s, 3 s and 10 s
    if (await loadUpdate()) return;
    for (const wait of [3000, 7000]) {
      await new Promise((r) => setTimeout(r, wait));
      if (await loadUpdate()) return;
    }
  }

  async function installUpdate() {
    const v = updateInfo.latest.version;
    const yes = await ask({ title: tr("update.install"), text: tr("settings.update_confirm", { v }), ok: tr("update.install") });
    if (!yes) return;
    const btn = $("#update-install");
    btn.disabled = true;
    $("#update-text").textContent = tr("settings.update_downloading");
    showError($("#top-error"), null);
    try {
      await api("POST", "/api/update/install", {});
    } catch (e) {
      btn.disabled = false;
      $("#update-download").hidden = false; // a way forward: the release page
      $("#update-text").textContent = tr("update.available", { v });
      showError($("#top-error"), e);
      return;
    }
    document.body.textContent = tr("settings.update_installing");
    document.body.className = "quit-done";
  }

  function bindUpdate() {
    $("#update-install").addEventListener("click", installUpdate);
    $("#update-download").addEventListener("click", () => {
      if (updateInfo.latest) window.open(updateInfo.latest.page, "_blank", "noopener");
    });
    $("#update-dismiss").addEventListener("click", () => {
      if (updateInfo.latest) remember("updateDismissed", updateInfo.latest.version);
      $("#update-bar").hidden = true;
    });
  }

  async function saveSettings() {
    const dlg = $("#settings");
    const body = {};
    $$("[data-setting]", dlg).forEach((el) => { body[el.dataset.setting] = el.value; });
    body.keep_sources = $("#s-keep_sources").checked;
    body.check_updates = $("#s-check_updates").checked;
    const langChanged = body.ui_lang !== BOOT.lang;
    stopHotkeyRecord(false);
    let saved;
    try {
      saved = await api("POST", "/api/settings", body);
    } catch (e) {
      showError($("#settings-error"), e);
      return;
    }
    state.clip.keepSources = body.keep_sources;
    if (saved && saved.live_hotkey) { // in its canonical form ("ctrl+f9" comes back as "Ctrl+F9")
      state.live.hotkey = saved.live_hotkey;
      renderCapture();
    }
    if (langChanged) {
      BOOT.lang = body.ui_lang;
      document.documentElement.lang = body.ui_lang;
      try { await loadStrings(body.ui_lang); } catch (e) { showError($("#top-error"), e); }
      applyI18n();
      renderVersion();
      rerenderDynamic();
    }
    dlg.close();
    $("#form-status").textContent = tr("settings.saved");
    loadVoices(); // a new key may unlock voices
  }

  // Installs the newest yt-dlp; it is used after a restart (the note says so in both outcomes).
  async function updateYtdlp() {
    const btn = $("#ytdlp-update");
    btn.disabled = true;
    showError($("#ytdlp-error"), null);
    $("#ytdlp-status").textContent = tr("settings.ytdlp_running");
    try {
      const { version } = await api("POST", "/api/ytdlp/update", {});
      $("#ytdlp-status").textContent = tr("settings.ytdlp_done", { version });
    } catch (e) {
      $("#ytdlp-status").textContent = "";
      showError($("#ytdlp-error"), e);
    } finally {
      btn.disabled = false;
    }
  }

  function rerenderDynamic() {
    renderMusicSelect();
    renderDerived();
    renderClipDerived();
    renderClipReview();
    renderReview();
    $$("#live-channels .channel, #live-moments .lm").forEach((n) => applyI18n(n));
    renderChannels();
    renderCapture();
    renderNotices();
    renderMoments();
    setHotkeyHint(state.live.rec ? "live.hotkey_recording_hint" : "live.hotkey_hint");
    $$("#queue-list .job, #clip-jobs-list .job").forEach((li) => {
      applyI18n(li);
      const job = state.jobs.find((j) => j.id === li.dataset.id);
      if (job) updateJob(li, job);
    });
    renderProgress();
  }

  // ---------- wiring ----------

  function bind() {
    bindHelpToggles();

    $("#profile").addEventListener("change", (e) => switchProfile(e.target.value));
    $("#profile-menu-btn").addEventListener("click", () => {
      if ($("#profile-menu").hidden) openProfileMenu(); else closeProfileMenu(true);
    });
    $("#profile-menu").addEventListener("keydown", onMenuKey);
    $("#profile-menu").addEventListener("click", (e) => {
      const b = e.target.closest("[data-profile-act]");
      if (b) profileAction(b.dataset.profileAct);
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".profile")) closeProfileMenu();
    });

    $$('input[name="mode"]').forEach((r) => r.addEventListener("change", () => {
      const batch = mode() === "batch";
      $("#single-box").hidden = batch;
      $("#batch-box").hidden = !batch;
      (batch ? $("#topics") : $("#topic")).focus();
    }));
    $("#topic").addEventListener("keydown", (e) => { if (e.key === "Enter" && !state.busy) onCreate(); });

    const form = $(".form");
    form.addEventListener("input", onFormInput);
    form.addEventListener("change", onFormInput);

    $("#write-script").addEventListener("click", onWrite);
    $("#create").addEventListener("click", onCreate);
    $("#discard-review").addEventListener("click", () => {
      state.review = null;
      renderReview();
      $("#review-progress").textContent = "";
      $("#write-script").focus();
    });

    $(".navtabs").addEventListener("click", (e) => {
      const b = e.target.closest('[role="tab"]');
      if (b) setView(b.dataset.view);
    });
    $(".navtabs").addEventListener("keydown", onTabKey);
    document.addEventListener("visibilitychange", syncLiveShown);

    $("#live-add").addEventListener("click", addChannel);
    $("#live-url").addEventListener("keydown", (e) => { if (e.key === "Enter") addChannel(); });
    $("#live-mark").addEventListener("click", markMoment);
    $("#live-stop").addEventListener("click", stopCapture);
    $("#live-export").addEventListener("click", exportLiveMoments);
    $("#live-aspect").addEventListener("change", (e) => { // the same format as Clipping and Crear short
      if (!state.current) return;
      state.current.aspect = e.target.value;
      fillForm();
      if (state.current.language) scheduleSave();
    });

    const clipPanel = $(".form-clip");
    clipPanel.addEventListener("input", onClipInput);
    clipPanel.addEventListener("change", onClipInput);
    pillFor($("#c-kind"));
    pillFor($("#c-framing"));
    bindPops();
    $("#c-more").addEventListener("click", openOptions);
    $("#c-options-close").addEventListener("click", closeOptions);
    $("#c-options").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeOptions(); }); // the backdrop
    $("#c-options").addEventListener("close", () => $("#c-more").focus());
    $("#c-file-btn").addEventListener("click", () => $("#c-file").click());
    $("#c-file").addEventListener("change", (e) => {
      const file = e.target.files[0];
      e.target.value = ""; // the same file can be chosen again
      startUpload(file);
    });
    $("#upload-cancel").addEventListener("click", cancelUpload);
    $("#c-chat-btn").addEventListener("click", () => $("#c-chat-file").click());
    $("#c-chat-file").addEventListener("change", (e) => {
      const file = e.target.files[0];
      e.target.value = ""; // the same file can be chosen again
      readChat(file);
    });
    $("#c-chat-remove").addEventListener("click", () => { setChat(null); $("#c-chat-btn").focus(); });
    $("#c-source-remove").addEventListener("click", () => { dropSource(); $("#c-url").focus(); });
    bindDrop();
    $("#c-url").addEventListener("keydown", (e) => { if (e.key === "Enter" && !state.busy) onClipRun("auto"); });
    $("#clip-find").addEventListener("click", () => onClipRun("review"));
    $("#clip-create").addEventListener("click", () => onClipRun("auto"));
    $("#clip-export").addEventListener("click", onExport);
    $("#clip-discard").addEventListener("click", discardAnalysis);

    $("#queue-list").addEventListener("click", onJobAction);
    $("#clip-jobs-list").addEventListener("click", onJobAction);
    $("#clips-close").addEventListener("click", closeClips);
    for (const ev of ["cancel", "close"]) { // Esc fires both; the close button runs closeClips
      $("#clips-view").addEventListener(ev, unloadClips);
    }
    $("#clips-open").addEventListener("click", async () => {
      try {
        await api("POST", `/api/jobs/${encodeURIComponent($("#clips-view").dataset.job)}/open-folder`, {});
        showError($("#clips-error"), null);
      } catch (err) {
        showError($("#clips-error"), err);
      }
    });
    // the server clears every finished job, whatever its kind: both buttons do the same
    for (const [btn, err, title] of [["#clear-finished", "#queue-error", "#queue-title"], ["#clip-jobs-clear", "#clip-jobs-error", "#clip-jobs-title"]]) {
      $(btn).addEventListener("click", async () => {
        try { await api("POST", "/api/jobs/clear", {}); } catch (e) { showError($(err), e); }
        await refreshJobs();
        $(title).focus();
      });
    }

    $("#open-settings").addEventListener("click", openSettings);
    $("#settings-save").addEventListener("click", saveSettings);
    $("#quit-app").addEventListener("click", quitApp);
    bindUpdate();
    $("#llm-models-refresh").addEventListener("click", loadModels);
    $("#s-llm_provider").addEventListener("change", () => {
      $("#s-llm_model").value = ""; // a model name belongs to one provider
      loadModels();
    });
    $("#s-llm_model_pick").addEventListener("change", () => syncModelInput(true));
    $("#ytdlp-update").addEventListener("click", updateYtdlp);
    $("#s-hotkey-record").addEventListener("click", () => {
      if (state.live.rec) stopHotkeyRecord(false); else startHotkeyRecord();
    });
    $("#s-hotkey-record").addEventListener("blur", () => stopHotkeyRecord(false));
    document.addEventListener("keydown", onHotkeyKey, true);
    $("#settings").addEventListener("cancel", (e) => { if (state.live.rec) e.preventDefault(); });
    $("#settings").addEventListener("close", () => stopHotkeyRecord(false));
    $$("#settings [data-close]").forEach((b) => b.addEventListener("click", () => $("#settings").close()));
    $$("#settings .show-btn").forEach((b) => b.addEventListener("click", () => {
      setShown(b, b.getAttribute("aria-pressed") !== "true");
    }));

    window.addEventListener("pagehide", () => {
      if (!state.saveTimer) return;
      clearTimeout(state.saveTimer);
      fetch("/api/profiles", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
        body: JSON.stringify(state.current) });
    });
  }

  async function init() {
    bind();
    setView(savedView());
    try {
      await loadStrings(BOOT.lang);
      renderVersion();
      state.live.ready = true;
      syncLiveShown();
      loadHotkey();
      await Promise.all([loadProfiles(), loadMusic()]);
      fillForm();
      fillClip();
      loadVoices();
    } catch (e) {
      showError($("#top-error"), e);
    }
    refreshJobs();
    pollStatus(); // also while another tab is shown: a hotkey press without a capture is explained there
    pollUpdate();
  }

  init();
})();
