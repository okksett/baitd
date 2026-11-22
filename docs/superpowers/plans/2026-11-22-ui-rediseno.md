# Rediseño de la interfaz (estilo C, Clips primero) — plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Baitd con el estilo C (oscuro, cristal, títulos grandes), menú lateral, nombres para público de clips y una pantalla Clips simplificada a fondo.

**Architecture:** Solo cambia la capa web: `index.html` (Jinja), `app.css`, `app.js` (sin framework) y los textos `i18n/es.json` y `en.json`. Los `id` de los controles se conservan para que `app.js` siga leyendo y guardando el perfil igual. Las listas `<select>` siguen siendo la fuente de verdad; las píldoras visibles son botones que las reflejan.

**Tech Stack:** Flask + Jinja, JavaScript sin framework, CSS propio, pytest (`tests/test_server.py` comprueba el HTML, el JS y las claves de i18n).

**Spec:** `docs/superpowers/specs/2026-11-22-ui-rediseno-design.md`

## Global Constraints

- Sin dependencias nuevas; nada se carga de internet (fuentes: Inter, Montserrat ExtraBold, Anton y Noto Sans, las que ya trae `app.css`).
- Cero emojis en `index.html`, `app.js` e i18n. Los símbolos usados como icono (⚙ ⋯ ✕ ▾) pasan a SVG en línea con `aria-hidden="true"` o a texto. La flecha → de los mensajes de error ("causa → arreglo") se queda: es texto, no un icono.
- Se conservan todos los `id` que usa `app.js`, salvo los que el plan quita a propósito: `tab-*`, `subtab-*`, `s-theme`, `[data-theme-set]` y los `sum-*` de Clips.
- Texto de la app nunca por debajo de 12 px; el texto normal entre 14 y 15 px.
- Todo texto visible sale de i18n (`test_index_has_no_hardcoded_copy`); `es.json` y `en.json` tienen las mismas claves (`test_i18n_keys_match`).
- Valores guardados sin cambios: `framing`, `kind`, `count` (0 = Todos), `aspect`.
- Archivos con fin de línea LF. Escribir con la herramienta de edición, no con `Path.write_text` de Python en Windows (convierte a CRLF).
- No tocar `.impeccable/`.
- Commits: solo si Mauricio autorizó los commits de este plan. Si no, cada paso "Commit" se salta y el trabajo queda sin commit en la rama.
- Python de las pruebas: `C:/Users/zacli/AppData/Local/Temp/claude/46a585e4df8b1555/b9b081ba-18dd-4507-aa5f-a4a92af6859e/scratchpad/venv/Scripts/python.exe -m pytest` (abreviado `PYTEST` abajo).

## Review Focus

1. **Un perfil guardado con valores fuera de lo común** (`framing: "partido"`, `count: 0`, `aspect: "1:1"`, `kind: "historia"`): al abrir Clips, cada píldora muestra ese valor, no el primero de la lista. La Task 4 lo comprueba en el navegador.
2. **Teclado:** el panel Más opciones y el editor de momento se cierran con Esc y el foco vuelve al botón que los abrió; el menú lateral se recorre con flechas arriba y abajo. Tasks 2, 4 y 6.
3. **Ventana angosta (700 px):** nada se sale de la pantalla en horizontal en Clips, En vivo, Short con IA y Ajustes. Task 7.
4. **Inglés:** con `ui_lang: en`, las etiquetas más largas ("Pick the moments myself", "Gameplay + cam") no rompen las píldoras ni los botones. Task 7.
5. **Trabajos mezclados:** con un short y un clip en cola a la vez, el short aparece solo en la columna de Short con IA y el clip solo en Tus clips; la barra de progreso de la columna de Short ignora los trabajos de clips. Task 5.

---

### Task 1: Tema oscuro único y estilo base

**Files:**
- Modify: `src/baitd/web/static/app.css` (variables `:root`, bloques `[data-theme="light"]` y `prefers-color-scheme: light`, `body`, `.panel`, `.b1`, `.b2`, `.pill`, `.input`)
- Modify: `src/baitd/web/templates/index.html` (`data-theme` del `<html>`, `.modetog` del header, `s-theme` en Ajustes)
- Modify: `src/baitd/web/static/app.js` (sección theme, `openSettings` línea ~2370, `saveSettings`, `bind`)
- Modify: `src/baitd/web/server.py:407` (deja de pasar `theme=`)
- Test: `tests/test_server.py` (`test_index_uses_saved_language_and_theme`, `test_index_theme_unset_on_first_run`)

**Interfaces:**
- Produces: variables CSS que usan las tareas siguientes: `--bg #0c0c12`, `--glass rgba(255,255,255,.07)`, `--glass-line rgba(255,255,255,.14)`, `--text #fff`, `--muted rgba(255,255,255,.6)`, `--radius 22px`, `--pill 999px`; clase `.glass` (fondo `--glass`, borde `--glass-line`, `backdrop-filter: blur(14px)`, `border-radius: var(--radius)`); `.b2` = botón principal blanco con texto `--bg`; `.b1` = botón de contorno.
- El fondo de `body`: `radial-gradient(circle at 78% 0%, #3b2a7a 0, transparent 45%), radial-gradient(circle at 8% 100%, #1d3b6b 0, transparent 40%), #0c0c12`, fijo (`background-attachment: fixed`).

- [ ] **Step 1: Write the failing test** — reemplaza las dos pruebas de tema por esta:

```python
def test_index_is_dark_only(client):
    client.post("/api/settings", json={"ui_lang": "en", "theme": "light"})
    html = client.get("/").text
    assert 'lang="en"' in html and 'data-theme="dark"' in html
    assert "data-theme-set" not in html and 'id="s-theme"' not in html
    css = CSS.read_text(encoding="utf-8")
    assert 'data-theme="light"' not in css and "prefers-color-scheme: light" not in css
```

- [ ] **Step 2: Run it to see it fail** — `PYTEST tests/test_server.py -k dark_only -q` → FAIL (`data-theme="light"` en el HTML).
- [ ] **Step 3: Implement** — `<html ... data-theme="dark">` fijo; quitar `.modetog`, `s-theme` y su etiqueta; quitar `lightQuery`, `effectiveTheme`, `renderTheme`, `setTheme`, sus llamadas y el envío de `theme` en `saveSettings`; borrar los bloques de tema claro del CSS y poner las variables de arriba. El ajuste `theme` del servidor se queda (compatibilidad), solo se ignora.
- [ ] **Step 4: Run** — `PYTEST tests/test_server.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "ui: tema oscuro único con el estilo C"`.

### Task 2: Menú lateral en lugar de pestañas

**Files:**
- Modify: `index.html` (header `.top` → `<aside class="nav">`; quitar `.subtabs` de `panel-clip`)
- Modify: `app.js` (`setTab`, `setSubTab`, `onTabKey`, `init` líneas ~2743-2744, `openReview` línea ~1212, `bind` líneas ~2643-2648)
- Modify: `app.css` (rejilla de `.app`: menú de ~220 px + área principal; barra arriba por debajo de 760 px)
- Modify: `i18n/es.json`, `i18n/en.json`
- Test: `tests/test_server.py` (`test_index_has_clipping_tab_and_drop_zone` líneas 547-551, `test_index_has_live_tab` líneas 589-591)

**Interfaces:**
- Produces: `setView(view: "clips" | "live" | "short", focus = false)`, que reemplaza a `setTab` y `setSubTab`. Muestra `#panel-short` para `short`, y `#panel-clip` con `#panel-video` (`clips`) o `#panel-live` (`live`). Mantiene `state.tab` (`"short"`/`"clip"`) y `state.live.sub` (`"video"`/`"live"`) para el resto del código, llama a `syncLiveShown()` y guarda `remember("view", view)`. Al arrancar usa el valor guardado de `view`; si no hay, lo deduce de los antiguos `tab`/`subtab`.
- Botones: `<nav>` con `role="tablist"` y `aria-orientation="vertical"`; `id="nav-clips"`, `"nav-live"` y `"nav-short"`, `role="tab"`, `data-view`, `aria-controls` hacia su panel. Las flechas arriba y abajo, Home y End se mueven entre ellos. Al pie: el perfil (`#profile`, `#profile-menu-btn`, `#profile-menu`) y `#open-settings` con un icono de engranaje en SVG más el texto `settings.open`.
- i18n nuevas: `nav.clips` "Clips"/"Clips", `nav.live` "En vivo"/"Live", `nav.short` "Short con IA"/"AI Short", `nav.label` "Secciones"/"Sections". Se borran `tab.short`, `tab.clip`, `live.tab_video`, `live.tab_live`, `live.subtabs` y `tabs.label` si ya nada las usa.

- [ ] **Step 1: Failing test** — en las dos pruebas, cambia las comprobaciones de pestañas por:

```python
    assert re.search(r'<nav[^>]*role="tablist"[^>]*aria-orientation="vertical"', html)
    for view, panel in (("clips", "panel-clip"), ("live", "panel-clip"), ("short", "panel-short")):
        assert re.search(rf'<button[^>]*role="tab"[^>]*id="nav-{view}"[^>]*aria-controls="{panel}"', html), view
    assert 'id="panel-video"' in html and 'id="panel-live"' in html and "subtab-" not in html
    assert es["nav.clips"] in html and es["nav.live"] in html and es["nav.short"] in html
```

- [ ] **Step 2: Run** — `PYTEST tests/test_server.py -k "clipping_tab or live_tab" -q` → FAIL.
- [ ] **Step 3: Implement `setView`** y el marcado del menú. `openReview` llama a `setView("clips")`.
- [ ] **Step 4: Run** — `PYTEST tests/test_server.py -q` → PASS.
- [ ] **Step 5: Check in the browser** (`preview_start` "baitd"): los 3 botones cambian de panel, las flechas se mueven entre ellos, recargar la página conserva la sección.
- [ ] **Step 6: Commit** — `ui: menú lateral Clips, En vivo y Short con IA`.

### Task 3: Nombres nuevos, Gameplay en Corte y cero emojis

**Files:**
- Modify: `i18n/es.json`, `i18n/en.json` (valores de la tabla "Nombres" del spec)
- Modify: `index.html` (`#c-framing` gana `<option value="partido" data-i18n="framing.partido">`; los símbolos ⚙ ⋯ ✕ pasan a SVG)
- Modify: `app.js` (cualquier texto o símbolo puesto a mano que sea un emoji o uno de esos iconos)
- Test: `tests/test_server.py` (nueva)

**Interfaces:**
- Claves que cambian de valor (es / en): `clip.find` "Elegir yo los momentos" / "Pick the moments myself"; `clip.create` "Crear clips" / "Make clips"; `clip.kind` "¿Qué buscas?" / "What are you after?"; `clip.kind.todos` "Todo" / "Everything"; `clip.kind.gracioso` "Risas" / "Laughs"; `clip.kind.interesante` "Épico" / "Epic"; `clip.kind.polemico` "Polémica" / "Drama"; `clip.kind.historia` "Historia" / "Story"; `framing.title` "Corte" / "Crop"; `framing.cara` "Sigue la cara" / "Follow the face"; `framing.centro` "Centro" / "Center"; `framing.completo` "Todo el video" / "Whole video"; `framing.partido` "Gameplay + cámara" / "Gameplay + cam"; `extras.title` "En el clip" / "In the clip"; `extras.hook` "Título arriba" / "Top title"; `extras.credit` "@ del canal" / "Channel @"; `queue.title` "Tus clips" / "Your clips" (solo en Clips; la columna de Short usa la clave nueva `queue.short_title` "Cola" / "Queue").
- Produces: un macro de Jinja `icon(name)` en `index.html` (o un `<template id="tpl-icons">` con `<symbol>` + `<use href="#i-name">`) con `settings`, `more`, `close`, `chevron` y `arrow`: trazo de 1.75 px en `currentColor`, 18 px, `aria-hidden="true"`.

- [ ] **Step 1: Failing test**

```python
_EMOJI = re.compile("[\u2600-\u27BF\U0001F000-\U0001FAFF\u2B00-\u2BFF\u22EF\u25BE]")  # \u2192 stays: errors read "cause \u2192 fix"

def test_no_emojis_or_symbol_icons_in_the_ui():
    es = json.loads((WEB / "static/i18n/es.json").read_text(encoding="utf-8"))
    en = json.loads((WEB / "static/i18n/en.json").read_text(encoding="utf-8"))
    for name, text in (("index", INDEX.read_text(encoding="utf-8")), ("js", (WEB / "static/app.js").read_text(encoding="utf-8")),
                       ("es", json.dumps(es, ensure_ascii=False)), ("en", json.dumps(en, ensure_ascii=False))):
        assert not _EMOJI.search(text), (name, _EMOJI.search(text).group())
    assert es["framing.partido"] == "Gameplay + cámara" and es["clip.kind.interesante"] == "Épico"
    assert '<option value="partido" data-i18n="framing.partido">' in INDEX.read_text(encoding="utf-8").split('id="c-framing"')[1].split("</select>")[0]
```

- [ ] **Step 2: Run** — `PYTEST tests/test_server.py -k no_emojis -q` → FAIL. Si el patrón choca con un carácter legítimo de un texto (por ejemplo "·"), el patrón no lo incluye: "·" es U+00B7 y queda fuera.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `PYTEST tests/test_server.py -q` → PASS.
- [ ] **Step 5: Commit** — `ui: nombres para público de clips, Gameplay en Corte y sin emojis`.

### Task 4: Pantalla Clips: cuadro grande, 3 píldoras y Más opciones

**Files:**
- Modify: `index.html` (`#panel-video` hasta `#clip-status`)
- Modify: `app.js` (`fillClip`, `renderClipDerived`, `onClipInput`, `bind`; nuevas `pillFor` y `openOptions`/`closeOptions`)
- Modify: `app.css`
- Modify: i18n (`clip.head_a` "Haz", `clip.head_b` "clips." o como encaje el título "Haz clips."; `clip.subtitle` "Pega un video largo y saca sus mejores momentos." / "Paste a long video and get its best moments."; `clip.more` "Más opciones" / "More options"; `clip.advanced` "Avanzado" / "Advanced"; `clip.url_placeholder` "Pega el link o suelta tu video aquí…" / "Paste the link or drop your video here…")
- Test: `tests/test_server.py` (nueva)

**Interfaces:**
- Orden dentro de `#panel-video`: título `#clip-head` con su subtítulo; el cuadro `.glass` `#drop` con `#c-url`, `#c-file-btn` + `#c-file`, `#upload-box`, `#c-source`, la fila de píldoras y los botones `#clip-find` (`.b1`) y `#clip-create` (`.b2`); debajo, `.rights`, `#clip-error` y `#clip-status`; luego `#clip-jobs` (Task 5) y `#clip-review` (Task 6).
- Píldoras visibles:
  - formato: `#c-aspect` con aspecto de píldora;
  - duración: el botón `#c-len-pill` abre un pequeño panel con `#c-min`, `#c-max`, `#c-len-help-btn` y `#c-len-help`;
  - cantidad: el botón `#c-count-pill` abre un panel con `#c-count`, `#c-count-all`, `#c-count-help-btn` y `#c-count-help`.
  El texto de las píldoras de duración y cantidad lo pone `fillClip` ("20–60 s", "5 clips" o "Todos").
- `#c-more` abre `<dialog id="c-options" class="sheet">` con `showModal()`, pegado a la derecha. Lleva un botón de cerrar `#c-options-close` y en este orden:
  1. `#c-kind` y `#c-framing`, como grupos de píldoras;
  2. Subtítulos: `#c-preset`, `#c-size`, `#c-position`, `#c-color` y `#c-highlight`;
  3. En el clip: `#c-hook`, `#c-credit`, `#c-music` y `#c-music-hint`;
  4. el chat: `#c-chat-btn`, `#c-chat-hint`, `#c-chat-file` y `#c-chat`;
  5. `<details id="c-advanced">` cerrado, con `#c-language`, `#c-transcriber`, `#c-model`, `#c-trans-hint`, `#c-fps` y `#c-fps-hint`.
  Al cerrar (Esc, ✕ o clic fuera) el foco vuelve a `#c-more`. Los cambios dentro se guardan como hoy: el panel va dentro de `#clip-settings`, así que `onClipInput` los sigue viendo.
- `pillFor(select: HTMLSelectElement) -> HTMLElement`: genera un grupo de botones `aria-pressed`, uno por `<option>`, después de la lista. La lista queda oculta (`hidden`) pero sigue en el formulario. Al pulsar un botón se fija `select.value` y se lanza `change` con `bubbles`. `fillClip` llama a `syncPills()`, que pone `aria-pressed` según `select.value`. Se usa para `#c-kind` y `#c-framing`.
- Desaparecen de Clips los acordeones `acc-trans`, `acc-framing`, `acc-csubs` y `acc-extras` y los `sum-trans`, `sum-framing`, `sum-csubs` y `sum-extras`. `renderClipDerived` conserva solo `#c-model.disabled` y `#c-fps-hint.hidden`.

- [ ] **Step 1: Failing test**

```python
def test_clips_screen_shows_three_choices_and_hides_the_rest(client):
    html = client.get("/").text
    main, sheet = html.split('id="c-options"')[0], html.split('id="c-options"')[1].split("</dialog>")[0]
    for vis in ("c-url", "c-file-btn", "c-aspect", "c-len-pill", "c-count-pill", "c-more", "clip-find", "clip-create"):
        assert f'id="{vis}"' in main.split('id="panel-video"')[1], vis
    for hidden in ("c-kind", "c-framing", "c-preset", "c-hook", "c-credit", "c-music", "c-chat-btn", "c-language",
                   "c-transcriber", "c-model", "c-fps"):
        assert f'id="{hidden}"' in sheet, hidden
    assert 'id="sum-trans"' not in html and 'id="acc-trans"' not in html
```

- [ ] **Step 2: Run** — `PYTEST tests/test_server.py -k three_choices -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `PYTEST tests/test_server.py -q` → PASS, incluida la lista de campos de `test_index_has_clipping_tab_and_drop_zone`.
- [ ] **Step 5: Check in the browser:**
  - Review Focus 1: pon a mano en el perfil `framing: partido`, `count: 0`, `aspect: 1:1` y `kind: historia`; recarga y comprueba que cada píldora lo muestra.
  - Abre y cierra Más opciones con Esc y comprueba que el foco vuelve a `#c-more`.
  - Cambia el Corte dentro del panel, recarga y comprueba que se guardó.
  - Sube un archivo y comprueba que no sale "Falta el video".
- [ ] **Step 6: Commit** — `ui: pantalla Clips simplificada con Más opciones`.

### Task 5: Tus clips en Clips; la Cola de Short solo con shorts

**Files:**
- Modify: `index.html` (nueva sección `#clip-jobs` en `#panel-video`; la columna `.side` solo se ve en Short con IA)
- Modify: `app.js` (`renderJobs`, `renderProgress`, `renderPreview`, `onJobAction`, `setView`)
- Modify: `app.css` (`#clip-jobs` como rejilla de tarjetas de cristal; un trabajo en marcha ocupa todo el ancho)
- Modify: i18n (`clips.empty_list` "Aquí aparecen tus clips." / "Your clips show up here.", `queue.short_title`)
- Test: `tests/test_server.py`

**Interfaces:**
- `renderJobList(list: HTMLElement, jobs: object[], empty: HTMLElement, clear: HTMLElement)`: el cuerpo actual de `renderJobs` (reutiliza `#tpl-job` y `updateJob` sin cambios). `renderJobs()` la llama dos veces:
  - con `#queue-list`, `#queue-empty` y `#clear-finished` para los trabajos `!isClipJob(job)`;
  - con `#clip-jobs-list`, `#clip-jobs-empty` y `#clip-jobs-clear` para los trabajos `isClipJob(job)`.
- En `#clip-jobs-list`, los trabajos activos van primero (`ACTIVE`) y luego el resto en el orden de `state.jobs`, el más nuevo primero.
- `renderProgress` y `renderPreview` solo miran los trabajos `!isClipJob`.
- `onJobAction` se ata a las dos listas. Después de quitar un trabajo, el foco va al título de la lista donde estaba: `#queue-title` o `#clip-jobs-title`.
- `.side` (vista previa del teléfono y Cola) se muestra solo cuando la vista es `short`.

- [ ] **Step 1: Failing test**

```python
def test_clip_jobs_live_in_the_clips_screen(client):
    html = client.get("/").text
    video = html.split('id="panel-video"')[1].split('id="panel-live"')[0]
    for i in ("clip-jobs", "clip-jobs-title", "clip-jobs-list", "clip-jobs-empty", "clip-jobs-clear"):
        assert f'id="{i}"' in video, i
    js = (WEB / "static/app.js").read_text(encoding="utf-8")
    assert "function renderJobList(" in js and "isClipJob" in js.split("function renderProgress(")[1].split("function ")[0]
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `PYTEST tests/test_server.py -q` → PASS.
- [ ] **Step 5: Check in the browser** (Review Focus 5): pon en cola un short con un guion de prueba y un clip de un archivo corto. Comprueba que cada uno aparece en su lista y que Cancelar, Ver clips y Quitar funcionan en Tus clips.
- [ ] **Step 6: Commit** — `ui: Tus clips en la pantalla Clips`.

### Task 6: Elegir momentos en tarjetas y editor en panel lateral

**Files:**
- Modify: `index.html` (`#clip-review` y `#tpl-moment`; nuevo `<dialog id="moment-editor" class="sheet wide">`)
- Modify: `app.js` (`openReview`, `renderClipReview`, `momentCard`, `renderExportState`)
- Modify: `app.css`
- Modify: i18n (`clip.review_title` → "{n} momentos encontrados" / "{n} moments found"; `clip.review_hint` → "Marca los que quieras. Toca uno para verlo y moverle el inicio o el final." / "Tick the ones you want. Tap one to watch it and move its start or end."; `clip.export` → "Exportar {n} clips" / "Export {n} clips")
- Test: `tests/test_server.py`

**Interfaces:**
- Mientras `#clip-review` está abierto, `#panel-video` oculta el título, el cuadro y `#clip-jobs` (clase `reviewing` en `#panel-video`). Al exportar o descartar se quita esa clase.
- Cabecera: `#clip-review-title` (con `n`), `#clip-selected`, `#clip-review-source`, el texto de ayuda, `#clip-keep-note`, `#clip-export` (`.b2`, con `n` marcados) y `#clip-discard` (`.b1`). `#clip-why` y `#clip-review-error` van debajo de la cabecera.
- `#moment-list` es una rejilla de 2 columnas (1 por debajo de 760 px). Cada tarjeta tiene en su cara visible: casilla, miniatura (color plano si no hay fotograma), título, tipo, duración y minuto del video.
- `momentCard(m, idx)` sigue construyendo el editor completo de hoy, dentro de un contenedor `.moment-editor` de la tarjeta. Al tocar la cara visible, `openMomentEditor(card)` mueve ese contenedor a `#moment-editor` y abre el diálogo. Al cerrarlo, `closeMomentEditor()` lo devuelve a la tarjeta y pone el foco en ella. Se mueve el mismo nodo, así que no se pierden los listeners (`bindCamEditor` incluido).

- [ ] **Step 1: Failing test**

```python
def test_moment_editor_opens_in_a_side_sheet(client):
    html = client.get("/").text
    assert '<dialog id="moment-editor"' in html
    tpl = html.split('id="tpl-moment"')[1].split("</template>")[0]
    assert 'class="moment-editor"' in tpl
    es = json.loads((WEB / "static/i18n/es.json").read_text(encoding="utf-8"))
    assert "{n}" in es["clip.review_title"] and "{n}" in es["clip.export"]
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — `PYTEST tests/test_server.py -q` → PASS.
- [ ] **Step 5: Check in the browser** con un análisis ya guardado (`analysis_or_404` lee `analisis/<id>.json`; usa uno existente o crea uno con `tests/test_clip_pipeline.py::_llm` como guía):
  - abre el editor, cambia el inicio y cierra con Esc; el cambio sigue en la tarjeta;
  - en un momento Gameplay, mueve el cuadro de la cámara;
  - exporta 1 y comprueba que sale en Tus clips.
- [ ] **Step 6: Commit** — `ui: elegir momentos en tarjetas con editor lateral`.

### Task 7: En vivo, Short con IA y Ajustes con el estilo nuevo; ventana angosta e inglés

**Files:**
- Modify: `app.css` (paneles `.panel`, `.live-block`, `.capture`, `.review`, `.acc`, `dialog#settings`, `#clips-view`, `.update-bar`, `.err`; media query a 760 px)
- Modify: `index.html` (solo clases y los iconos de la Task 3 que falten)

**Interfaces:**
- Consumes: las variables y `.glass` de la Task 1. No cambia el contenido ni el orden de En vivo, Short con IA ni Ajustes.

- [ ] **Step 1: Check in the browser at 1280 × 800 and 700 × 900**, en español y en inglés (`/api/settings` con `ui_lang`), con este script en cada vista:

```js
[...document.querySelectorAll("body *")].filter(e => { const r = e.getBoundingClientRect(); return r.width && r.right > innerWidth + 1; }).map(e => e.id || e.className)
```

  Esperado: `[]` en las 4 vistas (Clips, En vivo, Short con IA y Ajustes abierto). Vuelve a poner `ui_lang` como estaba.
- [ ] **Step 2: Fix** lo que el script o la vista muestren (desbordes, texto de menos de 12 px, contraste bajo sobre el cristal).
- [ ] **Step 3: Run** — `PYTEST -q` (suite completa con el filtro por defecto) → PASS.
- [ ] **Step 4: Commit** — `ui: estilo nuevo en En vivo, Short con IA y Ajustes`.

### Task 8: Comprobación final

- [ ] **Step 1:** `PYTEST -q` → todo PASS (las pruebas de red siguen fuera por el `-m "not network"` de `pyproject.toml`).
- [ ] **Step 2:** Captura de Clips, Más opciones, Tus clips con un trabajo en marcha y Elegir momentos a 1280 px. Compara con las maquetas `.superpowers/brainstorm/643-1795346597/content/flujo-clips-v2.html` y anota en el informe lo que difiera a propósito.
- [ ] **Step 3:** `git diff --stat main...HEAD` solo toca `src/baitd/web/`, `src/baitd/web/server.py`, `tests/test_server.py` y los archivos de docs de este plan, más lo que ya traía la rama `fix/archivo-y-clips`.
