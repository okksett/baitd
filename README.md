# Baitd

**Español** · [English](#english)

## Qué es

Baitd es una aplicación gratuita y de código abierto (licencia MIT) que convierte **un tema** (por ejemplo, "los beneficios de leer") en un **video corto listo para publicar** en TikTok, YouTube Shorts o Reels. La IA escribe el guion con gancho, se buscan clips de stock gratuitos, se genera la voz, se queman subtítulos palabra por palabra y se añade música de fondo. Todo corre en tu ordenador; la interfaz solo escucha en `127.0.0.1`.

## Instalar

### Opción A: descargar la app (la más fácil)

1. Ve a la página de **Releases** del repositorio y descarga el paquete de tu sistema (Windows, macOS Intel / Apple Silicon o Linux).
2. Descomprímelo y abre Baitd con doble clic. Se abre el navegador con la app.
3. Primer arranque: la app no está firmada, así que tu sistema avisará (ver abajo).

**Windows (SmartScreen):** si aparece "Windows protegió su PC", pulsa **Más información → Ejecutar de todas formas**.

**macOS (Gatekeeper):** haz clic derecho sobre la app y elige **Abrir**, y confirma. Si no aparece esa opción, ve a **Ajustes → Privacidad y seguridad**, baja hasta el aviso de Baitd y pulsa **Abrir igualmente**.

### Opción B: desde el código

Necesitas Python 3.10 o superior ([python.org/downloads](https://www.python.org/downloads/)). FFmpeg viene incluido.

1. Descarga o clona el repositorio.
2. **Windows:** doble clic en `iniciar.bat`. **macOS / Linux:** `./iniciar.sh`.
3. La primera vez crea `.venv` e instala las dependencias; luego abre el navegador en la app.

Opciones del comando: `python -m baitd --no-browser` (solo muestra la URL) y `--port 8765` (puerto fijo).

> **Windows:** mantén la carpeta en una ruta corta, por ejemplo `C:\proyectos\baitd`. Las rutas muy largas (y las carpetas sincronizadas como OneDrive) hacen fallar la creación del entorno.

## Cómo conseguir cada clave

Las claves se pegan en **Ajustes** y solo se envían al servicio al que pertenecen. Solo necesitas las de los servicios que vayas a usar.

| Servicio | Para qué | Dónde conseguirla |
|---|---|---|
| Pexels | Clips y fotos de stock (**necesaria**, gratis) | <https://www.pexels.com/api/> |
| Pixabay | Más clips de stock (opcional, gratis) | <https://pixabay.com/api/docs/> |
| Gemini | Guion con IA, nivel gratuito | <https://aistudio.google.com/app/apikey> |
| OpenAI | Guion con IA y voces (de pago) | <https://platform.openai.com/api-keys> |
| ElevenLabs | Voces de más calidad (de pago) | <https://elevenlabs.io/app/settings/api-keys> |
| Ollama | Guion con IA local, sin clave | <https://ollama.com> |

## El camino gratis

1. Consigue una clave gratuita de **Pexels**.
2. Para el guion, usa el nivel gratuito de **Gemini** (o instala **Ollama** y corre un modelo en tu equipo, sin clave).
3. Para la voz, usa **edge-tts** (por defecto, sin clave).
4. Música y fuentes ya vienen incluidas.

> **Aviso sobre edge-tts:** usa el servicio de voces de Microsoft Edge, que **no es oficial** y podría dejar de funcionar en cualquier momento. Si falla, cambia a otra voz (OpenAI, ElevenLabs) en Ajustes.

## Licencias de lo incluido

- **Programa:** MIT (ver `LICENSE`).
- **Música:** pistas de FreePD, dominio público (CC0), recuperadas de Internet Archive. Detalle en `src/baitd/assets/music/CREDITS.md`.
- **Fuentes:** licencia OFL. Detalle en `src/baitd/assets/fonts/LICENSES`.

## Dónde se guardan tus datos

Ajustes, claves, perfiles, cola, caché de clips, música propia y videos viven fuera de la app, en la carpeta de datos del usuario (`platformdirs.user_data_dir("Baitd")`), así que sobreviven a las actualizaciones:

- **Windows:** `C:\Users\<tú>\AppData\Local\Baitd`
- **macOS:** `~/Library/Application Support/Baitd`
- **Linux:** `~/.local/share/Baitd`

El registro de errores es `baitd.log` en esa misma carpeta. Puedes cambiarla con la variable de entorno `BAITD_DATA`.

---

<a id="english"></a>

# Baitd (English)

## What it is

Baitd is a free, open-source (MIT) app that turns **a topic** (e.g. "the benefits of reading") into a **ready-to-post short video** for TikTok, YouTube Shorts or Reels. An LLM writes a script with a hook, free stock clips are found, a voiceover is generated, word-by-word captions are burned in and background music is added. Everything runs on your machine; the UI only listens on `127.0.0.1`.

## Install

### Option A: download the app (easiest)

1. Open the repository's **Releases** page and download the package for your system (Windows, macOS Intel / Apple Silicon, or Linux).
2. Unzip it and double-click Baitd. Your browser opens on the app.
3. First launch: the app is unsigned, so your system will warn you (see below).

**Windows (SmartScreen):** if you see "Windows protected your PC", click **More info → Run anyway**.

**macOS (Gatekeeper):** right-click the app, choose **Open**, and confirm. If that option is missing, go to **System Settings → Privacy & Security**, scroll to the Baitd notice and click **Open Anyway**.

### Option B: run from source

You need Python 3.10 or newer ([python.org/downloads](https://www.python.org/downloads/)). FFmpeg is bundled.

1. Download or clone the repository.
2. **Windows:** double-click `iniciar.bat`. **macOS / Linux:** `./iniciar.sh`.
3. The first run creates `.venv` and installs dependencies, then opens the app in your browser.

Command options: `python -m baitd --no-browser` (only print the URL) and `--port 8765` (fixed port).

> **Windows:** keep the folder in a short path such as `C:\proyectos\baitd`. Very long paths (and synced folders like OneDrive) can break virtual-environment creation.

## Getting each API key

Paste keys in **Settings (Ajustes)**; each is only sent to the service it belongs to. You only need keys for the services you use.

| Service | Used for | Get it here |
|---|---|---|
| Pexels | Stock clips and photos (**required**, free) | <https://www.pexels.com/api/> |
| Pixabay | More stock clips (optional, free) | <https://pixabay.com/api/docs/> |
| Gemini | LLM script, free tier | <https://aistudio.google.com/app/apikey> |
| OpenAI | LLM script and voices (paid) | <https://platform.openai.com/api-keys> |
| ElevenLabs | Higher-quality voices (paid) | <https://elevenlabs.io/app/settings/api-keys> |
| Ollama | Local LLM, no key | <https://ollama.com> |

## The free path

1. Get a free **Pexels** key.
2. For the script, use the **Gemini** free tier (or install **Ollama** and run a model locally, no key).
3. For the voice, use **edge-tts** (default, no key).
4. Music and fonts are already bundled.

> **edge-tts warning:** it uses Microsoft Edge's voice service, which is **unofficial** and may stop working at any time. If it fails, switch to another voice provider (OpenAI, ElevenLabs) in Settings.

## Bundled licences

- **App:** MIT (see `LICENSE`).
- **Music:** FreePD tracks, public domain (CC0), retrieved via the Internet Archive. See `src/baitd/assets/music/CREDITS.md`.
- **Fonts:** OFL. See `src/baitd/assets/fonts/LICENSES`.

## Where your data lives

Settings, keys, profiles, queue, clip cache, your own music and videos are stored outside the app, in the user data folder (`platformdirs.user_data_dir("Baitd")`), so they survive updates:

- **Windows:** `C:\Users\<you>\AppData\Local\Baitd`
- **macOS:** `~/Library/Application Support/Baitd`
- **Linux:** `~/.local/share/Baitd`

Errors are logged to `baitd.log` in that folder. Override the location with the `BAITD_DATA` environment variable.
