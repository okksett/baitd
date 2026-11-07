# Baitd: apartado de Clipping. Documento de diseño

Fecha: 2026-11-07 · Estado: borrador para aprobar
Se construye sobre Baitd v1 (rama `feat/v1`, diseño en `docs/diseno-2026-11-07.md`). Todo lo que este documento no cambia sigue igual que en ese diseño.

Las marcas **[decisión de Claude]** señalan lo que eligió Claude. Lo demás lo pidió quien pidió el proyecto.

## 1. Qué es y por qué

La app pasa a llamarse **Baitd** y tiene dos apartados:

1. **Crear short**: lo que ya hace Baitd. Recibe un tema y entrega un video corto con guion, voz, clips de stock, subtítulos y música.
2. **Clipping** (nuevo): recorta un video largo en clips verticales listos para TikTok, Reels y Shorts.
   - **Fuentes:** YouTube, streams, podcasts y similares.
   - **Uso:** subir los clips para conseguir visitas e interacciones.
   - **Contenido:** sirve para material propio, para campañas de clipping (un creador paga o da permiso) y para material de canales ajenos.

Sigue siendo una herramienta gratuita, abierta (MIT) y sin fines de lucro. Cada persona la descarga y usa sus propias claves en su propia computadora.

## 2. Metas

- **Entrada flexible:** subir un **archivo** de video o pegar un **enlace**, a elección del usuario.
- **Control total:** el usuario elige **cuántos clips**, la **duración** mínima y máxima, el idioma, el formato y los fps, y los demás ajustes.
- **Elegir buenos momentos:** la app encuentra los momentos que dan visitas: **graciosos, interesantes, polémicos**, cuando se habla de algo interesante, o al estilo *clipfarming*.
- **Clip terminado:** cada clip sale editado y listo para subir: vertical, con subtítulos palabra por palabra, con el audio normalizado y, si se quiere, con el crédito de la fuente.
- **Dos modos**, igual que en Crear short:
  - **Automático:** exporta los N mejores clips.
  - **Con revisión:** muestra los candidatos y el usuario elige cuáles exportar.
- **Cambio de nombre:** la app completa pasa de Baitd a Baitd.

## 3. Lo que NO es (no-metas para esta versión)

- No acepta como fuente enlaces de **TikTok, Reels ni Shorts**, porque esos ya son el destino. La app los rechaza con un mensaje claro **[decisión de Claude]**.
- **No publica** en redes. El usuario sube los clips a mano.
- No descarga contenido que pida iniciar sesión, de pago o protegido (DRM). Tampoco usa cookies del navegador.
- No recorta transmisiones **en vivo** mientras ocurren, solo videos ya terminados.
- No reconoce quién habla cuando hay varias personas: no hay detección del hablante activo ni pantalla dividida automática (queda para una v2) **[decisión de Claude]**.
- No traduce los clips a otro idioma.

## 4. Anti-metas: qué lo haría fracasar aunque funcione

Valen las mismas de siempre: **"que no atrape, no enganche y se vea de mala calidad en todo ámbito"**. En un clip, eso significa:

| Fracaso | Barrera concreta que lo evita |
|---|---|
| No atrapa | El clip **empieza en el gancho**: la primera frase ya tiene que despertar interés, no puede ser un "bueno, entonces…". Se puede añadir un **título de gancho** en texto durante los primeros segundos **[decisión de Claude]**. |
| Corta lo importante | Cada clip **empieza y termina en frases completas** y nunca corta el remate de un chiste ni la conclusión de una idea. |
| No engancha | Duración dentro del rango elegido. Se recortan los silencios largos (más de 0,6 s) **[decisión de Claude]**. Los subtítulos mantienen la vista ocupada. |
| Encuadre malo | En vertical, el encuadre **sigue la cara** de quien está en pantalla con un movimiento suave, sin saltos ni temblores. Nunca deja una cara cortada por la mitad. En un cambio de plano del video original, el encuadre salta junto con el corte, no se desliza. |
| Imagen de mala calidad | Se descarga la **mejor resolución disponible** y nunca se deforma la imagen. En Clipping, el límite de ampliación sube a **1,8×**, que es lo necesario para pasar un video horizontal de 1080p a vertical, y se aplica un enfoque suave. Si el original es menor de 1080p (habría que ampliar más de 1,8×), el clip usa automáticamente el **encuadre completo con fondo difuminado** (ver §6.5). Lo eligió quien pidió el proyecto. |
| Audio de mala calidad | Se usa el audio original normalizado a ≈ −14 LUFS, sin saturación, en AAC a 192 kbps o más. Por defecto los clips no llevan música. |
| Subtítulos malos | Las mismas reglas que en Crear short: palabra por palabra, desfase de 150 ms como máximo, legibles y dentro de la zona segura. |

## 5. Restricciones

- Valen todas las del diseño original: local, privado, multiplataforma, FFmpeg incluido, app de doble clic, datos fuera de la app y hardware común.
- **Descarga de enlaces**: se hace con **yt-dlp** (licencia libre Unlicense), incluido en la app. Funciona con cientos de sitios. YouTube cambia seguido y puede romper la descarga, así que la app avisa cuando falla y propone subir el archivo en su lugar. Además, yt-dlp se puede actualizar desde Ajustes **[decisión de Claude]**.
- **Archivos grandes**: hasta **4 horas** de video por fuente. Se sube desde el navegador al servidor local, sin pasar por internet.
- **Dónde se guarda el original** **[decisión de Claude]**:
  - Los originales van a una carpeta `fuentes/`, aparte de la caché de clips de stock, y **no cuentan para el tope de 3 GB**.
  - Antes de descargar, la app comprueba que haya espacio libre suficiente (el tamaño estimado más un 20 %). Si no lo hay, avisa con el formato "causa → qué hacer".
  - El original se borra cuando se exportaron todos los clips de esa fuente, salvo que el usuario marque **"Guardar el original"** (desmarcado por defecto). Para volver a recortar un original borrado, hay que descargarlo otra vez.
- **Ruta gratis**: la transcripción puede hacerse **en la propia computadora** con faster-whisper (licencia MIT), sin clave. Es más lenta y la interfaz lo avisa ("transcripción local: más lenta"). El modelo se descarga una sola vez, la primera vez que se usa.
- **Peso de la app de doble clic** **[decisión de Claude]**:
  - El paquete incluye yt-dlp, faster-whisper (con CTranslate2) y la detección de caras de OpenCV (el modelo YuNet, que pesa menos de 1 MB).
  - El modelo de Whisper (≈ 250 MB el "small") **no va dentro**: se descarga la primera vez que se usa la transcripción local.
  - Meta: un paquete de unos **400 MB** como máximo por sistema, en los tres sistemas y las dos arquitecturas de Mac.
- **Tiempos objetivo** en un equipo de gama medio **[decisión de Claude]**, para un podcast de 1 hora con 5 clips y sin contar la descarga:
  - con transcripción por API: **15 minutos o menos**;
  - totalmente gratis: **45 minutos o menos**.
- **Aviso honesto**: con videos ajenos, respetar los derechos de autor y las reglas de cada plataforma es responsabilidad de quien usa la app. La interfaz lo dice una vez, en el apartado, sin bloquear nada.

## 6. Cómo funciona

### 6.1 Flujo

1. El usuario abre la pestaña **Clipping**, elige perfil y sube un archivo o pega un enlace.
2. Elige:
   - **cuántos clips** (de 1 a 20; por defecto, 5);
   - la **duración** mínima y máxima (por defecto, de 20 a 60 s; entre 10 y 180 s);
   - el **tipo de momento** (todos, gracioso, interesante, polémico o dato/historia);
   - el **idioma**, que se detecta solo y se puede cambiar;
   - el **formato** (9:16 por defecto) y los **fps** (30 o 60, sin superar los del original).
3. Pulsa **"Buscar momentos"** (con revisión) o **"Crear clips"** (automático).
4. La app sigue estos pasos, cada uno con su barra de progreso:
   - descarga o lectura del archivo
   - transcripción
   - búsqueda de momentos
   - encuadre
   - render
5. **Con revisión**: aparece una lista de candidatos, cada uno con:
   - una vista previa que reproduce ese tramo del original;
   - su puntuación y su tipo;
   - el título sugerido;
   - el texto.

   El usuario marca cuáles quiere, ajusta el inicio y el fin a mano si lo necesita, y pulsa **"Exportar"**.
6. **Cola** **[decisión de Claude]**: la misma cola persistente de Crear short, una tarea a la vez.
   - **Automático**: una fuente es **una tarea** de la cola, y los clips son sus pasos internos.
   - **Con revisión**: hay dos tareas.
     1. **"Analizar"**: descarga, transcribe y busca momentos. Al terminar, guarda en disco la transcripción y los candidatos y **libera la cola**.
     2. **"Exportar"**: los clips elegidos entran como tarea nueva.
   - Si la app se cierra durante la revisión, los candidatos siguen ahí al volver a abrirla. Se pueden revisar días después mientras el original exista.
7. **Perfiles** **[decisión de Claude]**: son los mismos de Crear short, con una sección nueva de clipping:
   - número de clips, rango de duración, tipo de momento y encuadre;
   - título de gancho, crédito y música;
   - proveedor de transcripción.

   El perfil da los valores por defecto, y en cada búsqueda se pueden cambiar. Los perfiles viejos reciben los valores por defecto de esta sección.

### 6.2 Entrada

- **Archivo**: MP4, MOV, MKV, WEBM o similares (todo lo que lea FFmpeg). También acepta solo audio (MP3, WAV, M4A). En ese caso, el clip lleva el **audiograma** **[decisión de Claude]**: un fondo de color del perfil, una onda animada y los subtítulos grandes.
- **Enlace**: yt-dlp descarga el video en la mejor calidad hasta 4K, junto con el título, el nombre del canal y la duración.

### 6.3 Transcripción

- **Proveedores**:
  - **OpenAI** con el modelo **whisper-1** (la clave que ya existe), que procesa el audio en partes por el límite de tamaño. Se usa whisper-1 porque es el que entrega marcas de tiempo por palabra **[decisión de Claude]**;
  - **Local** (faster-whisper; por defecto el modelo "small" y se puede cambiar a uno mayor en Ajustes).
- Siempre se guardan las **marcas de tiempo por palabra**, porque cortes y subtítulos dependen de ellas.
- Si el video tiene subtítulos propios en el sitio de origen, no se usan: Whisper da mejores marcas por palabra **[decisión de Claude]**.

### 6.4 Búsqueda de momentos

- La IA del perfil (OpenAI, Gemini o compatible) lee la transcripción por tramos que se solapan y propone momentos. Para cada uno da:
  - el inicio y el fin, alineados a frases;
  - el tipo (gracioso, interesante, polémico, dato/historia);
  - una **puntuación** de 0 a 100 según gancho, interés, remate y si se entiende sin contexto;
  - un **título de gancho** corto;
  - una descripción y hashtags para publicar.
- La **energía del audio** suma puntos: subidas de volumen y de ritmo, y risas cuando se detectan **[decisión de Claude: método libre]**.
- Se quitan los solapes y se ordena por puntuación. Con revisión se muestran hasta el doble de los clips pedidos, para tener de dónde elegir.
- Funciona con modelos de contexto chico (Ollama), porque se trabaja por tramos.

### 6.5 Encuadre (paso a vertical)

**[decisión de Claude]**. Para cada clip se elige uno de estos encuadres:

1. **Seguir cara** (por defecto): detecta las caras, elige la principal (la más grande y centrada) y mueve el recorte de forma suave. Cuando el video original cambia de plano, el encuadre se recalcula en ese corte.
2. **Centro**: un recorte fijo al centro, útil para pantallas y juegos.
3. **Completo con fondo difuminado**: el video entero cabe a lo ancho y el fondo se rellena con el mismo video difuminado. No amplía la imagen, así que mantiene la nitidez.

El modo se puede cambiar por clip en la revisión.

### 6.6 Edición del clip

- Se recortan los silencios mayores de 0,6 s dentro del clip.
- **Subtítulos**: los mismos estilos y ajustes que en Crear short, con los tiempos de la transcripción.
- **Título de gancho** opcional: un texto arriba durante los primeros 3 s, editable. Viene de la IA.
- **Crédito de la fuente** opcional: "Fuente: @canal". Se rellena solo con los datos del enlace, se puede editar y es discreto, en una esquina dentro de la zona segura **[decisión de Claude]**.
- **Música**: apagada por defecto. Si se enciende, va baja y con ducking, igual que en Crear short.

### 6.7 Interfaz

- Arriba, junto al logo, hay dos **pestañas**: **Crear short** y **Clipping**. La pantalla mantiene la misma estructura, colores, tipografías, modo oscuro y claro y vista previa que el boceto aprobado (`docs/mockups/ui-aprobada.html`) **[decisión de Claude]**.
- **Logo**: "BAIT*d*", con "BAIT" en Anton y la *d* en Instrument Serif cursiva, en el mismo estilo que tenía "BAIT*d*" **[decisión de Claude]**.
- En **Clipping**:
  - a la izquierda: el área para soltar un archivo o pegar un enlace, la fila con número de clips, duración y tipo, las filas plegables (**Transcripción**, **Encuadre**, **Subtítulos**, **Extras**: título, crédito y música) y los botones "Buscar momentos" y "Crear clips";
  - a la derecha: la vista previa vertical y, en modo revisión, la lista de candidatos.

### 6.8 Salida

- Hay una carpeta por fuente, `salidas/AAAA-MM-DD_titulo-del-video/`, con un archivo por clip: `clip-01.mp4`, `clip-02.mp4`, etc.
- En la misma carpeta va `publicacion.txt` con el título, la descripción y los hashtags de cada clip, y el tramo del original que usa (por ejemplo, 00:41:12–00:41:58).

### 6.9 Cambio de nombre a Baitd

- El nombre cambia en todas partes: el paquete de Python, el comando (`baitd`), los lanzadores, la app de doble clic, el README, la interfaz y la licencia.
- La carpeta de datos pasa a llamarse `Baitd`. Si al abrir la app existe la de Baitd y todavía no la de Baitd, se copian los perfiles, los ajustes y las claves **[decisión de Claude]**.

## 7. Cómo se ve el éxito

Una persona sin conocimientos técnicos debería poder:

1. Pegar el enlace de un podcast de 1 hora, pedir 5 clips de 30 a 60 s, irse y al volver encontrar 5 clips listos para subir.
2. Hacerlo totalmente gratis: Ollama o la capa gratis de Gemini, más transcripción local.
3. Ver en cada clip:
   - que empieza con algo que atrapa y termina con el remate;
   - que la cara está siempre bien encuadrada;
   - que los subtítulos van sincronizados;
   - que el audio está parejo;
   - que se ve nítido en el teléfono.
4. En modo revisión, descartar los candidatos malos y ajustar el inicio de uno en menos de un minuto.

## 8. Lo que decide quien construya el programa

- La librería de detección de caras (por ejemplo, MediaPipe u OpenCV) y cómo se suaviza el movimiento del encuadre.
- Cómo se detectan los cambios de plano (por ejemplo, el filtro `scdet` de FFmpeg o PySceneDetect).
- El método de energía del audio y de detección de risas.
- El formato exacto que la IA devuelve con los momentos, y cómo se reparte la transcripción en tramos.
- Cómo se sirve la vista previa de cada tramo en el navegador sin renderizar el clip entero.
- Cómo se migran los nombres en el código, y en qué orden se hace el cambio de nombre respecto al apartado nuevo.
- Los mensajes de error, que siempre siguen el formato "causa → qué hacer". Ejemplo: "YouTube rechazó la descarga → Actualiza yt-dlp en Ajustes o sube el archivo".
