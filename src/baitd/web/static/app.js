/* Baitd UI. Plain JS, no build step. Every visible string comes from i18n/<lang>.json via tr(). */
"use strict";

(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const BOOT = JSON.parse($("#boot").textContent);
  const KNOWN_LANGS = ["es", "en", "fr", "de"];
  const ACTIVE = ["pending", "running"];
  const FINISHED = ["done", "error", "cancelled"];
  const SAVE_DELAY = 500;
  const POLL_MS = 1000;
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
    if (!res.ok) throw new Error((data && data.error) || tr("err.network"));
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
    loadVoices();
  }

  function scheduleSave() {
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(() => { state.saveTimer = null; saveNow(); }, SAVE_DELAY);
  }

  function saveNow() {
    const snapshot = structuredClone(state.current);
    state.savePromise = api("POST", "/api/profiles", snapshot)
      .then((saved) => {
        const i = state.profiles.findIndex((p) => p.id === saved.id);
        if (i >= 0) state.profiles[i] = saved; else state.profiles.push(saved);
        showError($("#form-error"), null);
        return saved;
      })
      .catch((e) => { showError($("#form-error"), e); throw e; })
      .finally(() => { state.savePromise = null; });
    return state.savePromise;
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
    $("#f-preset").value = p.subtitle.preset;
    $("#f-size").value = String(p.subtitle.size);
    $("#f-color").value = p.subtitle.color.toLowerCase();
    $("#f-highlight").value = p.subtitle.highlight.toLowerCase();
    $("#f-position").value = p.subtitle.position;
    renderMusicSelect();
    $("#f-volume").value = String(Math.round(p.music_volume * 100));
    $("#f-llm").value = p.llm_provider;
    $("#f-model").value = p.llm_model;
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
    if (target && target.id === "f-preset") {
      p.subtitle = structuredClone(BOOT.presets[$("#f-preset").value]);
      $("#f-size").value = String(p.subtitle.size);
      $("#f-color").value = p.subtitle.color.toLowerCase();
      $("#f-highlight").value = p.subtitle.highlight.toLowerCase();
      $("#f-position").value = p.subtitle.position;
    } else {
      const size = parseInt($("#f-size").value, 10);
      if (size >= 24 && size <= 200) p.subtitle.size = size;
      p.subtitle.color = $("#f-color").value.toUpperCase();
      p.subtitle.highlight = $("#f-highlight").value.toUpperCase();
      p.subtitle.position = $("#f-position").value;
    }
    p.music = $("#f-music").value || "none";
    p.music_volume = Math.round(Number($("#f-volume").value)) / 100;
    p.llm_provider = $("#f-llm").value;
    p.llm_model = $("#f-model").value.trim();
    p.tone = $("#f-tone").value;
  }

  function onFormInput(e) {
    if (!state.current) return;
    const t = e.target;
    if (!t.closest || !t.closest(".form") || t.closest("#review")) return;
    if (!t.matches("select, input:not([type=radio]):not([type=checkbox]):not(#topic), textarea:not(#topics)")) return;
    readForm(t);
    renderDerived();
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
    $("#sum-ai").textContent = p.llm_model ? `${tr(`llm.${p.llm_provider}`)} · ${p.llm_model}` : tr(`llm.${p.llm_provider}`);
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
    $$("#review-list .script-retry").forEach((b) => { b.disabled = busy; });
    $(".form").setAttribute("aria-busy", String(busy));
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
    const first = $("#review-list .script-hook, #review-list .script-retry:not([hidden])");
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
    const body = $(".script-body", node);
    body.hidden = !item.script;
    if (!item.script) return node;
    const hookId = `hook-${idx}`;
    const hook = $(".script-hook", node);
    hook.id = hookId;
    $("label", body).htmlFor = hookId;
    hook.value = item.script.hook;
    hook.addEventListener("input", () => { item.script.hook = hook.value; });
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

  // ---------- queue ----------

  async function refreshJobs() {
    clearTimeout(state.pollTimer);
    try {
      const { jobs } = await api("GET", "/api/jobs");
      state.jobs = jobs;
      showError($("#queue-error"), null);
      renderJobs();
    } catch (e) {
      showError($("#queue-error"), e);
    }
    if (state.jobs.some((j) => ACTIVE.includes(j.status))) state.pollTimer = setTimeout(refreshJobs, POLL_MS);
  }

  function stepLabel(job) {
    if (job.status === "pending" || !job.step) return tr("step.waiting");
    return tr(`step.${job.step}`);
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
    $(".job-topic", li).textContent = job.topic;
    const chip = $(".chip", li);
    chip.textContent = tr(`status.${job.status}`);
    chip.dataset.status = job.status;
    const running = job.status === "running";
    $(".job-step", li).hidden = !ACTIVE.includes(job.status);
    $(".job-label", li).textContent = stepLabel(job);
    $(".job-count", li).textContent = running && job.step_index
      ? tr("progress.count", { n: job.step_index, total: BOOT.total_steps }) : "";
    const bar = $(".bar", li);
    bar.hidden = !running;
    $("i", bar).style.width = `${job.percent}%`;
    showError($(".job-err", li), job.error ? new Error(job.error) : null);
    $('[data-job-act="open"]', li).hidden = !(job.status === "done" && job.output);
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
    $("#progress-count").textContent = job.step_index
      ? tr("progress.count", { n: job.step_index, total: BOOT.total_steps }) : "";
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
    const url = {
      open: [`/api/jobs/${encodeURIComponent(id)}/open-folder`, "POST"],
      cancel: [`/api/jobs/${encodeURIComponent(id)}/cancel`, "POST"],
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
      $("#s-theme").value = effectiveTheme();
      $$(".show-btn", dlg).forEach((b) => setShown(b, false));
    } catch (e) {
      showError($("#top-error"), e);
      return;
    }
    dlg.showModal();
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

  async function saveSettings() {
    const dlg = $("#settings");
    const body = {};
    $$("[data-setting]", dlg).forEach((el) => { body[el.dataset.setting] = el.value; });
    const langChanged = body.ui_lang !== BOOT.lang;
    try {
      await api("POST", "/api/settings", body);
    } catch (e) {
      showError($("#settings-error"), e);
      return;
    }
    document.documentElement.dataset.theme = body.theme;
    renderTheme();
    if (langChanged) {
      BOOT.lang = body.ui_lang;
      document.documentElement.lang = body.ui_lang;
      try { await loadStrings(body.ui_lang); } catch (e) { showError($("#top-error"), e); }
      applyI18n();
      rerenderDynamic();
    }
    dlg.close();
    $("#form-status").textContent = tr("settings.saved");
    loadVoices(); // a new key may unlock voices
  }

  function rerenderDynamic() {
    renderMusicSelect();
    renderDerived();
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

    $("#queue-list").addEventListener("click", onJobAction);
    $("#clear-finished").addEventListener("click", async () => {
      try { await api("POST", "/api/jobs/clear", {}); } catch (e) { showError($("#queue-error"), e); }
      await refreshJobs();
      $("#queue-title").focus();
    });

    $("#open-settings").addEventListener("click", openSettings);
    $("#settings-save").addEventListener("click", saveSettings);
    $("#quit-app").addEventListener("click", quitApp);
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
    try {
      await loadStrings(BOOT.lang);
      await Promise.all([loadProfiles(), loadMusic()]);
      fillForm();
      loadVoices();
    } catch (e) {
      showError($("#top-error"), e);
    }
    refreshJobs();
  }

  init();
})();
