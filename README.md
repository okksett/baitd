# Baitd

**Español** · [English](#english)

## Qué es

Baitd es una aplicación gratuita y de código abierto (licencia MIT) que convierte **un tema** (por ejemplo, "los beneficios de leer") en un **video corto listo para publicar** en TikTok, YouTube Shorts o Reels, y saca **clips verticales de un video largo** (pestaña Clipping). La IA escribe el guion con gancho, se buscan clips de stock gratuitos, se genera la voz, se queman subtítulos palabra por palabra y se añade música de fondo. Todo corre en tu ordenador; la interfaz solo escucha en `127.0.0.1`.

## Instalar

### Opción A: descargar la app (la más fácil)

1. Ve a la página de **Releases** del repositorio y descarga el paquete de tu sistema (Windows, macOS Intel / Apple Silicon o Linux). Mientras no haya una Release, cada ejecución de **GitHub Actions** (pestaña *Actions* → workflow *build*) deja los paquetes como *artifacts*: `Baitd-windows-x64.zip`, `Baitd-macos-arm64.zip`, `Baitd-macos-x64.zip` y `Baitd-linux-x64.tar.gz`.
2. Descomprímelo y abre Baitd con doble clic. Se abre el navegador con la app.
3. Primer arranque: la app no está firmada, así que tu sistema avisará (ver abajo).

En Windows y Linux se abre también una ventana de consola con la dirección y "Pulsa Ctrl+C para cerrar"; en macOS la app no tiene consola y se cierra con **⚙ Ajustes → Salir de Baitd**.

**Si algo no funciona**, abre la app desde una terminal con `--selftest` (por ejemplo `Baitd.exe --selftest`): genera un vídeo de prueba sin conexión y sin tocar tus datos, y termina con `SELFTEST OK <segundos>` o con la causa del fallo.

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
| Pexels | Clips y fotos de stock. **Pexels o Pixabay: al menos una de las dos** (las dos son gratis) | <https://www.pexels.com/api/> |
| Pixabay | Clips y fotos de stock (alternativa o complemento a Pexels) | <https://pixabay.com/api/docs/> |
| Gemini | Guion con IA, nivel gratuito | <https://aistudio.google.com/app/apikey> |
| OpenAI | Guion con IA y voces (de pago) | <https://platform.openai.com/api-keys> |
| ElevenLabs | Voces de más calidad (de pago) | <https://elevenlabs.io/app/settings/api-keys> |
| Ollama | Guion con IA local, sin clave. En **Ajustes** pon la "Dirección de Ollama o servidor compatible" `http://localhost:11434/v1`; en el perfil, sección **Guion** ("IA que escribe el guion"), elige el proveedor "Ollama o compatible" | <https://ollama.com> |

## El camino gratis

1. Consigue una clave gratuita de **Pexels** o **Pixabay**.
2. Para el guion, usa el nivel gratuito de **Gemini** (o instala **Ollama** y corre un modelo en tu equipo, sin clave).
3. Para la voz, usa **edge-tts** (por defecto, sin clave).
4. Música y fuentes ya vienen incluidas.

> **Aviso sobre edge-tts:** usa el servicio de voces de Microsoft Edge, que **no es oficial** y podría dejar de funcionar en cualquier momento. Si falla, cambia de proveedor de voz (OpenAI, ElevenLabs) en la sección **Voz** del perfil; sus claves se pegan en **Ajustes**.

## Clipping: sacar clips de un video largo

La pestaña **Clipping** convierte un video largo (podcast, entrevista, stream) en varios clips verticales. Tiene dos modos: **Buscar momentos** (descarga, transcribe y propone candidatos; tú eliges y pulsas «Exportar seleccionados») y **Crear clips** (modo automático: elige los mejores momentos y los recorta, encuadra y subtitula sin revisión).

- **Enlaces y archivos:** pega un enlace (YouTube y cientos de sitios, con yt-dlp incluido) o sube un archivo desde el navegador; hasta 4 horas de video por fuente. Si YouTube rompe la descarga, la app avisa y puedes subir el archivo.
- **Transcripción:** con la **API de OpenAI** (whisper-1, de pago, usa la clave de Ajustes) o **local** con faster-whisper (gratis, sin clave, más lenta). Tiempos objetivo en un equipo de gama media, para un podcast de 1 hora con 5 clips y sin contar la descarga: con API, **15 minutos o menos**; totalmente gratis, **45 minutos o menos**.
- **Primera vez en local:** el modelo de Whisper ("small", unos **250 MB**) no viene dentro de la app; se descarga una sola vez al usar la transcripción local.
- **Encuadre vertical** (se cambia por clip en la revisión): **Cara** (por defecto, sigue a quien habla), **Centro** (recorte fijo al centro, útil para pantallas y juegos) y **Completo** (el video entero con fondo difuminado, sin ampliar).
- **Dónde se guarda:** los originales en `fuentes/` y las transcripciones y candidatos en `analisis/`, dentro de la carpeta de datos; los clips terminados van a `salidas/`. Los originales se borran al exportar todos sus clips, salvo que marques "Guardar videos originales" en Ajustes. No cuentan para el tope de 3 GB de la caché.
- **Actualizar yt-dlp:** si una descarga deja de funcionar, usa el botón **Actualizar yt-dlp** en **Ajustes** y reinicia Baitd.

> **Derechos:** si usas videos de otros canales, respeta sus derechos y las reglas de cada plataforma.

## Licencias de lo incluido

- **Programa:** MIT (ver `LICENSE`).
- **Música:** pistas de FreePD, dominio público (CC0), recuperadas de Internet Archive. Detalle en `src/baitd/assets/music/CREDITS.md`.
- **Fuentes:** licencia OFL. Detalle en `src/baitd/assets/fonts/LICENSES`.
- **Detección de caras:** modelo YuNet (licencia Apache 2.0). Detalle en `src/baitd/assets/models/LICENSE-yunet.txt`.

## Dónde se guardan tus datos

Ajustes, claves, perfiles, cola, caché de clips, música propia y videos viven fuera de la app, en la carpeta de datos del usuario (`platformdirs.user_data_dir("Baitd")`), así que sobreviven a las actualizaciones:

- **Windows:** `%LOCALAPPDATA%\Baitd\Baitd` (platformdirs repite el nombre)
- **macOS:** `~/Library/Application Support/Baitd`
- **Linux:** `~/.local/share/Baitd`

Dentro: `settings.json`, `profiles.json`, `queue.json`, `baitd.log`, `salidas/`, `musica/`, `cache/`, `fuentes/`, `analisis/`, `modelos/` y `yt-dlp.zip`. El registro de errores es `baitd.log` en esa misma carpeta. Puedes cambiarla con la variable de entorno `BAITD_DATA`.

**Caché de clips:** los clips descargados se guardan en `cache/` para no bajarlos dos veces. La caché tiene un límite de **3 GB**: al empezar cada vídeo se borran los clips usados hace más tiempo. Para cambiar el límite, edita `"cache_max_gb"` en `settings.json` (por ejemplo `"cache_max_gb": 5`) con la app cerrada. Puedes borrar la carpeta `cache/` cuando quieras.

---

<a id="english"></a>

# Baitd (English)

## What it is

Baitd is a free, open-source (MIT) app that turns **a topic** (e.g. "the benefits of reading") into a **ready-to-post short video** for TikTok, YouTube Shorts or Reels, and cuts **vertical clips from a long video** (Clipping tab). An LLM writes a script with a hook, free stock clips are found, a voiceover is generated, word-by-word captions are burned in and background music is added. Everything runs on your machine; the UI only listens on `127.0.0.1`.

## Install

### Option A: download the app (easiest)

1. Open the repository's **Releases** page and download the package for your system (Windows, macOS Intel / Apple Silicon, or Linux). Until there is a Release, every **GitHub Actions** run (*Actions* tab → *build* workflow) leaves the packages as *artifacts*: `Baitd-windows-x64.zip`, `Baitd-macos-arm64.zip`, `Baitd-macos-x64.zip` and `Baitd-linux-x64.tar.gz`.
2. Unzip it and double-click Baitd. Your browser opens on the app.
3. First launch: the app is unsigned, so your system will warn you (see below).

On Windows and Linux a console window also opens showing the address and "Pulsa Ctrl+C para cerrar" (press Ctrl+C to quit); on macOS the app has no console and quits from **⚙ Settings → Quit Baitd**.

**Troubleshooting:** run the app from a terminal with `--selftest` (e.g. `Baitd.exe --selftest`): it renders an offline test video without touching your data and ends with `SELFTEST OK <seconds>` or the cause of the failure.

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
| Pexels | Stock clips and photos. **Pexels or Pixabay: at least one of the two** (both are free) | <https://www.pexels.com/api/> |
| Pixabay | Stock clips and photos (alternative or complement to Pexels) | <https://pixabay.com/api/docs/> |
| Gemini | LLM script, free tier | <https://aistudio.google.com/app/apikey> |
| OpenAI | LLM script and voices (paid) | <https://platform.openai.com/api-keys> |
| ElevenLabs | Higher-quality voices (paid) | <https://elevenlabs.io/app/settings/api-keys> |
| Ollama | Local LLM, no key. In **Settings** set "Ollama or compatible server address" to `http://localhost:11434/v1`; in the profile's **Script** section ("AI that writes the script") pick the "Ollama or compatible" provider | <https://ollama.com> |

## The free path

1. Get a free **Pexels** or **Pixabay** key.
2. For the script, use the **Gemini** free tier (or install **Ollama** and run a model locally, no key).
3. For the voice, use **edge-tts** (default, no key).
4. Music and fonts are already bundled.

> **edge-tts warning:** it uses Microsoft Edge's voice service, which is **unofficial** and may stop working at any time. If it fails, switch to another voice provider (OpenAI, ElevenLabs) in the profile's **Voice** ("Voz") section; their keys go in **Settings**.

## Clipping: cut clips from a long video

The **Clipping** tab turns a long video (podcast, interview, stream) into several vertical clips. It has two modes: **Find moments** (download, transcribe and suggest candidates; you pick and press "Export selected") and **Create clips** (automatic mode: it picks the best moments and cuts, frames and captions them with no review).

- **Links and uploads:** paste a link (YouTube and hundreds of sites, yt-dlp is bundled) or upload a file from the browser; up to 4 hours of video per source. If YouTube breaks the download, the app tells you and you can upload the file instead.
- **Transcription:** through the **OpenAI API** (whisper-1, paid, uses the key from Settings) or **locally** with faster-whisper (free, no key, slower). Target times on a mid-range machine, for a 1-hour podcast with 5 clips and not counting the download: with the API, **15 minutes or less**; fully free, **45 minutes or less**.
- **First local run:** the Whisper model ("small", about **250 MB**) is not bundled; it is downloaded once when you first use local transcription.
- **Vertical framing** (changeable per clip during review): **Face** (default, follows the speaker), **Center** (fixed centre crop, good for screens and games) and **Full** (the whole video over a blurred background, no zoom).
- **Where things live:** sources in `fuentes/` and transcripts and candidates in `analisis/`, inside the data folder; finished clips go to `salidas/`. Sources are deleted once all their clips are exported unless you tick "Keep original videos" in Settings. They do not count toward the 3 GB cache cap.
- **Updating yt-dlp:** if downloads stop working, use the **Update yt-dlp** button in **Settings (Ajustes)** and restart Baitd.

> **Rights:** if you use other channels' videos, respect their rights and each platform's rules.

## Bundled licences

- **App:** MIT (see `LICENSE`).
- **Music:** FreePD tracks, public domain (CC0), retrieved via the Internet Archive. See `src/baitd/assets/music/CREDITS.md`.
- **Fonts:** OFL. See `src/baitd/assets/fonts/LICENSES`.
- **Face detection:** YuNet model (Apache 2.0). See `src/baitd/assets/models/LICENSE-yunet.txt`.

## Where your data lives

Settings, keys, profiles, queue, clip cache, your own music and videos are stored outside the app, in the user data folder (`platformdirs.user_data_dir("Baitd")`), so they survive updates:

- **Windows:** `%LOCALAPPDATA%\Baitd\Baitd` (platformdirs repeats the name)
- **macOS:** `~/Library/Application Support/Baitd`
- **Linux:** `~/.local/share/Baitd`

Inside: `settings.json`, `profiles.json`, `queue.json`, `baitd.log`, `salidas/`, `musica/`, `cache/`, `fuentes/`, `analisis/`, `modelos/` and `yt-dlp.zip`. Errors are logged to `baitd.log` in that folder. Override the location with the `BAITD_DATA` environment variable.

**Clip cache:** downloaded clips are kept in `cache/` so they are not fetched twice. The cache is capped at **3 GB**: when each video starts, the least recently used clips are deleted. To change the cap, edit `"cache_max_gb"` in `settings.json` (e.g. `"cache_max_gb": 5`) while the app is closed. You can delete the `cache/` folder at any time.
