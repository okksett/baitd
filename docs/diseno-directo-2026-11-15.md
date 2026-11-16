# Baitd: Clipping en directo. Documento de diseño

Fecha: 2026-11-15 · Estado: borrador para aprobar
Se construye sobre Baitd 0.4.1 (`main`). Todo lo que este documento no cambia sigue como está: Clipping de videos terminados (`docs/diseno-clipping-2026-11-07.md`), la ventana propia (0.4.0) y seguir a quien habla por voz y boca (0.3.2 y 0.4.1).

Las marcas **[decisión de Claude]** señalan lo que eligió Claude. Lo demás lo pidió quien pidió el proyecto (Mauricio).

## 1. Qué es y por qué

Hoy, para sacar un clip de un directo, Mauricio tiene que esperar a que termine, **descargar el stream completo** y buscar el momento a mano. Hace directos en **Kick, Twitch y YouTube**, sobre todo **IRL charlando frente a la cámara en su setup** y a veces **jugando con la cámara en una esquina**.

**Clipping en directo** saca los clips **mientras el directo ocurre**. Baitd sigue el directo publicado y guarda siempre los últimos minutos. Cuando pasa algo, el momento queda en una **lista para revisar** y exportar como cualquier clip de Baitd.

Lo que vale la pena clipear: algo **gracioso** o un **tema que la gente quiere saber** (humor o polémica).

Sirve para dos casos:

1. **Canal propio:** el streamer marca el momento con una **tecla rápida configurable**. Su chat suele ser tranquilo, así que la detección automática no le sirve; él sabe mejor que nadie cuándo pasó algo.
2. **Canales de otros streamers:** Baitd **vigila el directo solo** y propone los momentos. Esos clips llevan el **logo de la plataforma y el nombre del streamer**, para que se vea de dónde vienen.

La referencia de partida fue un video de otra app que hace algo parecido. Baitd hace lo suyo, con su propio nombre, estilo y diseño: no copia ni su marca ni su aspecto.

## 2. Metas

- **Elegir un directo:** el usuario guarda canales de Kick, Twitch y YouTube, propios y ajenos, y ve cuáles están **en vivo ahora**.
- **Búfer:** mientras un canal está "en captura", Baitd guarda los **últimos 5 minutos** del directo **[decisión de Claude]**, sin descargar el stream completo.
- **Marcar a mano (canal propio):** una **tecla rápida global** que funciona aunque el juego tenga el foco. Viene con F9 y se cambia en Ajustes.
  - Guarda **45 s antes y 15 s después** de pulsarla; los dos valores se ajustan en Ajustes **[decisión de Claude]**.
  - Si se vuelve a pulsar mientras se graban los segundos de después, **alarga** el clip otros 15 s.
  - También hay un botón en la ventana, con el mismo efecto.
- **Detección automática (canales ajenos):** Baitd propone momentos de **humor o polémica** sin que el usuario haga nada (ver §6.4).
- **Lista de revisión:** todo momento, automático o manual, cae en una lista con el **motivo** ("lo marcaste tú", "el chat explotó", "risas"). Desde ahí se ve, se ajustan inicio y fin, título y hashtags, y se exporta. Es la misma experiencia que "Buscar momentos" en Clipping **[decisión de Claude]**.
- **Clip terminado:** se exporta con lo que Baitd ya hace: vertical 9:16, subtítulos palabra por palabra, audio normalizado, título de gancho, y seguir a quien habla por voz o boca.
- **Dos formatos:**
  - **IRL en el setup:** el encuadre sigue la cara, como hoy.
  - **Juego con cámara en una esquina:** formato **partido**, con la cámara arriba y el juego abajo (§6.6).
- **Crédito en clips ajenos:** el logo de la plataforma junto al @nombre del streamer. Los clips del canal propio no lo llevan **[decisión de Claude]**.

## 3. Lo que NO es (no-metas)

- **No publica** en redes (TikTok, YouTube, etc.). El usuario sube los clips a mano. Ya se decidió así en una versión anterior.
- No toma el video de OBS ni de la pantalla: siempre usa el **directo publicado** en la plataforma **[decisión de Claude]**. Así funciona en la misma PC del stream o en otra, y el resultado es lo que ve el público.
- No graba el directo completo ni ofrece un archivo de todo el stream.
- No usa cuentas de las plataformas ni inicia sesión: lee el directo y el chat como un espectador anónimo.
- No hace detección automática en el canal propio. Si el usuario la quiere, puede marcar su canal como "de otros", pero no es el caso pensado.
- No traduce ni dobla.

## 4. Anti-metas: qué lo haría fracasar aunque funcione

| Fracaso | Barrera |
|---|---|
| Le quita fluidez al juego o al stream (corre en la misma PC) | Captura sin recodificar (copia del video tal cual). Lo pesado (transcribir, detectar caras) solo pasa en los tramos candidatos y al exportar, con **prioridad baja** del proceso **[decisión de Claude]**. Nada pesado corre de forma continua. |
| El clip no tiene el momento | El búfer cubre 5 min y la tecla guarda 45 s antes; el clip se ajusta en la revisión. |
| El clip sale con anuncios de Twitch ("Commercial break in progress") | Los trozos que Twitch marca como anuncio se descartan del búfer (§6.2). Si un clip igual cae sobre un anuncio, la revisión lo muestra con un aviso. |
| La tecla no responde dentro del juego | Tecla global del sistema, no del navegador ni de la ventana. |
| Gasta la cuota gratis de la IA en una hora | Una petición por candidato, no por minuto; un límite de peticiones por hora configurable (§6.4). |
| Llena el disco | El búfer es un anillo fijo de 5 min que se borra solo. Lo que no se exporta se borra al cerrar la captura, salvo los momentos marcados. |
| Calidad mala | Se captura la mejor calidad hasta la elegida en Ajustes (1080p por defecto); la exportación usa el mismo pipeline de Clipping. |

## 5. Restricciones

- Valen las de siempre: local, privado, gratis (MIT), multiplataforma, FFmpeg y yt-dlp incluidos, doble clic.
- **Plataformas:** Kick, Twitch y YouTube. yt-dlp ya lee los tres.
  - **Kick es imprescindible desde la primera versión:** Mauricio transmite ahí. Si yt-dlp no logra capturar su video, se construye lo necesario, por ejemplo un lector propio de la API de Kick y su HLS.
  - **Comprobado el 2026-11-15:** YouTube en vivo y Twitch en vivo dan video 1080p en HLS (m3u8).
  - Se grabaron 25 s de Twitch en trozos de 2 s y se unieron en un MP4 de 12 s con imagen y audio.
  - Kick no se pudo probar porque no había canales en vivo.
- **Anuncios de Twitch:** a un espectador sin suscripción, Twitch le mete sus anuncios dentro del video del directo. Hay que descartarlos (§6.2).
- **Cuota de IA:** el plan gratis de Gemini da 20 peticiones al día por modelo. La detección automática debe funcionar con pocas peticiones y degradar bien si se acaban (§6.4).
- **Tecla global:** en **Windows** es obligatoria. En **macOS** pide permiso de Accesibilidad; si no se concede, queda el botón de la ventana. En **Linux** es opcional. **[decisión de Claude]**
- **Misma PC del stream:** consumo continuo bajo (§4).

## 6. Cómo funciona

### 6.1 Canales

- En la pestaña **"En directo"** de Clipping el usuario agrega canales pegando su enlace: `kick.com/x`, `twitch.tv/x` o `youtube.com/@x`, de la misma forma tolerante con que hoy se pegan los enlaces.
- Cada canal tiene un interruptor **"es mío"** **[decisión de Claude]**:
  - mío → tecla rápida, sin crédito, sin detección automática;
  - de otro → detección automática y crédito.
- La lista muestra quién está **en vivo ahora**: título y, si la plataforma lo da, espectadores. Se actualiza cada pocos minutos y al abrir la pestaña.
- Se captura **un canal a la vez** **[decisión de Claude]**.
- **La captura la inicia el usuario:** abre Baitd, elige el canal y pulsa "Capturar". No hay inicio automático cuando un canal entra en vivo. Baitd tampoco queda corriendo en segundo plano: cerrar la ventana detiene la captura, como hoy cierra la app. Si la tecla se pulsa sin captura activa, no guarda nada y la ventana lo explica.

### 6.2 Captura y búfer

- Al empezar la captura, Baitd obtiene del directo la dirección HLS con yt-dlp, con la calidad elegida en Ajustes.
- Guarda los trozos del video tal cual, sin recodificar, en un anillo de **5 minutos** dentro de la carpeta de datos.
- **Anuncios de Twitch:** Twitch marca los trozos de anuncio en la lista HLS. Esos trozos no entran al búfer, así que el clip salta del antes al después del anuncio.
- **Reconexión:** si el directo se corta o cambia la dirección, Baitd vuelve a pedirla y sigue. Si el directo termina, la captura se detiene sola y avisa.
- La tecla funciona solo mientras hay una captura activa. Pulsarla sin captura no hace nada; la ventana lo explica.

### 6.3 Marcar a mano

- Al pulsar la tecla o el botón, el momento abarca desde 45 s antes hasta 15 s después.
- Cuando terminan los 15 s, el momento se arma con los trozos del búfer y queda en la lista como "lo marcaste tú".
- Volver a pulsar antes de que terminen los 15 s alarga el final otros 15 s. Hay un tope de 3 minutos por momento **[decisión de Claude]**.
- La ventana muestra en vivo "Guardando lo que sigue… 12 s · pulsa otra vez para alargar".

**Qué cuenta como "ahora"** **[decisión de Claude]**:
- El directo publicado va unos segundos detrás de la realidad: de 3 a 30 s según la plataforma y su ajuste de latencia.
- En el **canal propio**, "45 s antes" se cuenta desde el momento real en que el streamer pulsó la tecla, no desde lo que Baitd está recibiendo.
- Para eso Baitd relaciona el reloj de la PC con la hora que traen los trozos del directo (marcas de hora de la lista HLS, cuando la plataforma las da). Espera ese retraso **más** los 15 s; la cuenta atrás de la ventana muestra el tiempo real que falta.
- Si una plataforma no da esas marcas, se usa un **retraso por canal** que el usuario puede ajustar, con un valor de inicio razonable por plataforma.
- En **canales de otros**, la tecla y el botón también funcionan. Ahí "ahora" es lo último que recibió Baitd, y los 45 s de antes cubren el retraso de lo que el usuario estaba mirando.

**Qué se hace con un momento marcado a mano** **[decisión de Claude]**:
- Durante la captura, **nada pesado**: ni transcribir ni pedir a la IA. Solo se guarda el tramo y una miniatura. Así no se carga la PC del stream ni se gasta cuota.
- Al **revisarlo**, el usuario puede pedir título y hashtags a la IA, con un botón y una sola petición. Al **exportar**, se transcribe como cualquier clip.
- La IA **no mueve** el tramo que marcó el usuario. Al exportar, los bordes solo se ajustan a la frase completa más cercana, la regla de siempre de Clipping, para no cortar una palabra.

### 6.4 Detección automática (canales de otros)

En tres etapas, para gastar poco procesador y pocas peticiones:

1. **Señales baratas, todo el tiempo:**
   - **Chat:** mensajes por segundo y proporción de risas y emotes de risa, comparados con el ritmo habitual de ese chat en los últimos minutos. Un pico marca un candidato.
   - **Audio:** subidas fuertes de volumen y risas, comparadas con el nivel habitual del directo.
   - Si el chat de una plataforma no se puede leer, el audio basta.
2. **Transcribir solo el candidato:** unos 60 s alrededor del pico, con el mismo transcriptor que Clipping (local o API, según Ajustes), en prioridad baja.
3. **Una petición a la IA por candidato:** ¿es humor, polémica o nada?, más el inicio y fin del momento dentro del tramo, un título y hashtags. Si dice "nada", se descarta sin mostrarlo.

- **Límite:** como máximo **N peticiones por hora**, con N=6 de inicio, configurable **[decisión de Claude]**.
- Cuando no quedan peticiones, sea por el límite o porque se acabó la cuota, los candidatos entran a la lista **sin clasificar**, con el motivo de la señal ("el chat explotó"), y la IA se pide al revisarlos.
- Los candidatos que se pisan en el tiempo se juntan en uno.

### 6.5 Lista de revisión

- Es una lista por sesión de captura, con cada momento, su motivo, su hora y una miniatura.
- Desde ella se puede ver el momento, ajustar inicio y fin, editar título y hashtags, elegir el formato y exportar uno o varios.
- La exportación usa el mismo pipeline de Clipping: transcribe, encuadra, subtitula y renderiza, y entrega a la misma carpeta de salida.
- Los momentos se guardan aunque se cierre Baitd, y se borran cuando el usuario los quita o los exporta **[decisión de Claude]**. El resto del búfer se borra al terminar la captura.

### 6.6 Formatos

- **Cara (IRL en el setup):** como hoy, seguir la cara de quien habla, por voz y por boca.
- **Partido (juego con cámara):** arriba la cámara, abajo el juego.
  - Baitd encuentra la cámara solo: es una cara que aparece casi siempre en el mismo rincón del video. Recorta esa zona y la pone arriba; abajo va el juego recortado al centro de la acción, o al centro de la pantalla.
  - En la revisión se puede corregir el recuadro de la cámara si la detección se equivocó.
  - Si no encuentra una cámara, usa el formato Cara.
- Se elige por canal, con Cara por defecto. En cada momento se puede cambiar.

### 6.7 Crédito en clips de otros

- En una esquina va el **logo de la plataforma** (Kick, Twitch o YouTube) junto al **@nombre del streamer**, durante todo el clip, legible y dentro de la zona segura.
- Reemplaza al crédito de texto actual ("Fuente: @canal") en los clips de directo de otros.
- Los logos se usan solo como identificación de la fuente, en versión simple.

## 7. Lo que se deja al constructor

- Cómo leer la lista HLS y descartar los anuncios: lector propio de HLS o FFmpeg con filtro de trozos.
- Cómo leer el chat de cada plataforma:
  - Twitch: IRC anónimo;
  - Kick: su websocket público;
  - YouTube: el chat en vivo de la página.
  Si alguna resulta inviable, esa plataforma se queda solo con la señal de audio, y el documento de cierre lo dice.
- Cómo implementar la tecla global en cada sistema.
- Los umbrales exactos de los picos de chat y audio, calibrados con directos reales.
- Cómo se detecta y recorta la cámara en el formato Partido.
- El aspecto de la pestaña "En directo", con el estilo actual de Baitd.

## 8. Riesgos conocidos

- **Kick** no está probado: su extracción o su chat pueden pedir cosas que yt-dlp no resuelva. Como Kick es imprescindible, el plan empieza por probarlo con un canal en vivo.
- **Twitch** puede cambiar cómo marca los anuncios.
- Calibrar la detección automática necesita directos reales con chat activo. Es la parte con más incertidumbre.
- La tecla global en macOS depende del permiso de Accesibilidad.
