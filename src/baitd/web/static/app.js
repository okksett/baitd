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
      upload: null, // upload in flight: {id, xhr, cancelled}
      watch: new Set(), // clip_analyze jobs started from this page: their review opens when done
      review: null, // {analysisId, profileId, source, moments: [{...moment, orig, pick, raw}]}
      settingsBad: false, // a typed clip number is out of range (shown in #clip-error, not applied)
      saveFailed: false, // a profile save failed while this tab was shown (also in #clip-error)
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

  // ---------- theme ----------

  const lightQuery = window.matchMedia("(prefers-color-scheme: light)");

  function effectiveTheme() {
    return document.documentElement.dataset.theme || (lightQuery.matches ? "light" : "dark");
  }

  function renderTheme() {
    const t = effectiveTheme();
    $$("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
  }

  async function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    renderTheme();
    try {
      await api("POST", "/api/settings", { theme });
      showError($("#top-error"), null);
    } catch (e) {
      showError($("#top-error"), e);
    }
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

  // ---------- tabs ----------

  function setTab(tab, focus = false) {
    state.tab = tab === "clip" ? "clip" : "short";
    $$('[role="tab"]').forEach((b) => {
      const on = b.dataset.tab === state.tab;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    });
    $("#panel-short").hidden = state.tab !== "short";
    $("#panel-clip").hidden = state.tab !== "clip";
    $(".skip").setAttribute("href", state.tab === "clip" ? "#clip-head" : "#head");
    remember("tab", state.tab);
  }

  function onTabKey(e) {
    const tabs = $$('[role="tab"]');
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    const next = {
      ArrowRight: tabs[(i + 1) % tabs.length],
      ArrowLeft: tabs[(i - 1 + tabs.length) % tabs.length],
      Home: tabs[0],
      End: tabs[tabs.length - 1],
    }[e.key];
    if (!next) return;
    e.preventDefault();
    setTab(next.dataset.tab, true);
  }

  // ---------- clipping: settings (profile.clipping, plus the shared aspect, fps and subtitle) ----------

  const CLIP_LANGS = ["auto", "es", "en", "fr", "de", "it", "pt"];

  function fillClip() {
    const p = state.current;
    if (!p) return;
    const c = p.clipping;
    $("#c-count").value = String(c.count);
    $("#c-min").value = String(c.min_s);
    $("#c-max").value = String(c.max_s);
    $("#c-kind").value = c.kind;
    $("#c-aspect").value = p.aspect;
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
    renderClipDerived();
  }

  function renderClipDerived() {
    const p = state.current;
    if (!p) return;
    const c = p.clipping;
    $("#c-model").disabled = c.transcriber !== "local";
    $("#c-fps-hint").hidden = p.fps !== 60;
    $("#sum-trans").textContent = c.transcriber === "local"
      ? `${tr("transcriber.local")} · ${c.whisper_model}` : tr("transcriber.openai");
    $("#sum-framing").textContent = tr(`framing.${c.framing}`);
    $("#sum-csubs").textContent = `${tr(`preset.${p.subtitle.preset}`)} · ${tr(`pos.${p.subtitle.position}`)}`;
    const on = [c.hook_title && tr("extras.hook"), c.credit && tr("extras.credit"), c.music && tr("extras.music")];
    $("#sum-extras").textContent = on.filter(Boolean).join(" · ") || tr("extras.none");
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
    const count = int("#c-count");
    const min = int("#c-min");
    const max = int("#c-max");
    if (!(count >= 1 && count <= MAX_EXPORT)) return tr("err.clip_count");
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
      if (state.clip.source) body.source_id = state.clip.source.source_id;
      else if (!url) throw new Error(tr("err.clip_source"));
      else if (!/^https?:\/\/\S+$/i.test(url)) throw new Error(tr("err.clip_url"));
      else body.url = url;
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

  const reviewSignature = (r) => JSON.stringify(r.moments.map((m) => [m.orig, m.pick, m.title, m.start, m.end, m.framing, m.credit]));
  const reviewEdited = (r) => reviewSignature(r) !== r.baseline;

  async function openReview(job, userAsked) {
    const errBox = userAsked ? $("#queue-error") : $("#clip-error");
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
          analysisId: job.analysis_id, profileId, source: data.source,
          moments: ranked.map((m, i) => ({ ...m, title: m.title || "", framing: m.framing || "cara",
            credit: m.credit || "", pick: i < count, raw: {} })),
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
      setTab("clip");
      $("#clip-review-title").focus();
    }
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
    $(".m-score", node).textContent = tr("moment.score", { n: m.score });
    $(".m-kind", node).textContent = tr(`clip.kind.${m.kind}`);
    title.value = m.title;
    title.addEventListener("input", () => { m.title = title.value; named(); });
    framing.value = m.framing;
    framing.addEventListener("change", () => { m.framing = framing.value; });
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
    const moments = r.moments.filter((m) => m.pick).map((m) => ({
      orig: m.orig, start: m.start, end: m.end, framing: m.framing, title: m.title.trim(), credit: m.credit.trim() }));
    // the analysis was made with this profile; the selected one is only a fallback if it was deleted
    const profileId = profileById(r.profileId) ? r.profileId : state.current.id;
    setBusy(true);
    try {
      await flushSave(); // the export takes the hook title, credit and music switches from the saved profile
      await api("POST", `/api/analyses/${encodeURIComponent(r.analysisId)}/export`, { profile_id: profileId, moments });
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
    const list = $("#queue-list");
    const byId = new Map($$(".job", list).map((li) => [li.dataset.id, li]));
    const wanted = state.jobs.map((job) => {
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
    byId.forEach((li, id) => { if (!state.jobs.some((j) => j.id === id)) li.remove(); });
    wanted.forEach((li, i) => { if (list.children[i] !== li) list.insertBefore(li, list.children[i] || null); });
    $("#queue-empty").hidden = state.jobs.length > 0;
    $("#clear-finished").hidden = !state.jobs.some((j) => FINISHED.includes(j.status));
    renderProgress();
    renderPreview();
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
    $('[data-job-act="open"]', li).hidden = !(job.status === "done" && job.output);
    $('[data-job-act="retry"]', li).hidden = !RETRYABLE.includes(job.status);
    $('[data-job-act="cancel"]', li).hidden = !ACTIVE.includes(job.status);
    $('[data-job-act="remove"]', li).hidden = !FINISHED.includes(job.status);
  }

  function renderProgress() {
    const job = state.jobs.find((j) => j.status === "running") || state.jobs.find((j) => j.status === "pending");
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
    const latest = [...state.jobs].reverse().find((j) => j.status === "done" && j.has_video);
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

  async function onJobAction(e) {
    const btn = e.target.closest("[data-job-act]");
    if (!btn) return;
    const id = btn.closest(".job").dataset.id;
    const act = btn.dataset.jobAct;
    if (act === "review") {
      const job = state.jobs.find((j) => j.id === id);
      if (job) await openReview(job, true);
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
      showError($("#queue-error"), null);
    } catch (err) {
      showError($("#queue-error"), err);
    }
    if (act !== "open") {
      await refreshJobs();
      if (!document.contains(btn) || btn.hidden) $("#queue-title").focus(); // keep keyboard focus in the queue
    }
  }

  // ---------- settings dialog ----------

  async function openSettings() {
    const dlg = $("#settings");
    showError($("#settings-error"), null);
    try {
      const s = await api("GET", "/api/settings");
      $$("[data-setting]", dlg).forEach((el) => { el.value = s[el.dataset.setting] || ""; });
      loadModels(); // fills the model list for the saved provider; does not block the dialog
      $("#s-keep_sources").checked = s.keep_sources === true;
      $("#s-check_updates").checked = s.check_updates !== false;
      $("#ytdlp-status").textContent = "";
      showError($("#ytdlp-error"), null);
      $("#s-theme").value = effectiveTheme();
      dlg.dataset.themeShown = effectiveTheme();
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
    // saving a key must not pin the theme: unless it was changed here, the page keeps following the OS
    if (body.theme === dlg.dataset.themeShown) delete body.theme;
    const langChanged = body.ui_lang !== BOOT.lang;
    try {
      await api("POST", "/api/settings", body);
    } catch (e) {
      showError($("#settings-error"), e);
      return;
    }
    state.clip.keepSources = body.keep_sources;
    if (body.theme) {
      document.documentElement.dataset.theme = body.theme;
      renderTheme();
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
    $$("#queue-list .job").forEach((li) => {
      applyI18n(li);
      const job = state.jobs.find((j) => j.id === li.dataset.id);
      if (job) updateJob(li, job);
    });
    renderProgress();
  }

  // ---------- wiring ----------

  function bind() {
    bindHelpToggles();
    $$("[data-theme-set]").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeSet)));
    lightQuery.addEventListener("change", renderTheme);

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

    $(".tabs").addEventListener("click", (e) => {
      const b = e.target.closest('[role="tab"]');
      if (b) setTab(b.dataset.tab);
    });
    $(".tabs").addEventListener("keydown", onTabKey);

    const clipPanel = $(".form-clip");
    clipPanel.addEventListener("input", onClipInput);
    clipPanel.addEventListener("change", onClipInput);
    $("#c-file-btn").addEventListener("click", () => $("#c-file").click());
    $("#c-file").addEventListener("change", (e) => {
      const file = e.target.files[0];
      e.target.value = ""; // the same file can be chosen again
      startUpload(file);
    });
    $("#upload-cancel").addEventListener("click", cancelUpload);
    $("#c-source-remove").addEventListener("click", () => { dropSource(); $("#c-url").focus(); });
    bindDrop();
    $("#c-url").addEventListener("keydown", (e) => { if (e.key === "Enter" && !state.busy) onClipRun("auto"); });
    $("#clip-find").addEventListener("click", () => onClipRun("review"));
    $("#clip-create").addEventListener("click", () => onClipRun("auto"));
    $("#clip-export").addEventListener("click", onExport);
    $("#clip-discard").addEventListener("click", discardAnalysis);

    $("#queue-list").addEventListener("click", onJobAction);
    $("#clear-finished").addEventListener("click", async () => {
      try { await api("POST", "/api/jobs/clear", {}); } catch (e) { showError($("#queue-error"), e); }
      await refreshJobs();
      $("#queue-title").focus();
    });

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
    renderTheme();
    setTab(remembered("tab") || "short");
    try {
      await loadStrings(BOOT.lang);
      renderVersion();
      await Promise.all([loadProfiles(), loadMusic()]);
      fillForm();
      fillClip();
      loadVoices();
    } catch (e) {
      showError($("#top-error"), e);
    }
    refreshJobs();
    pollUpdate();
  }

  init();
})();
