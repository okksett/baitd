# Rediseño de la interfaz de Baitd (estilo C, Clips primero)

Fecha: 2026-11-22. Estado: para aprobar.

## Por qué

Baitd lo usa sobre todo **gente que recorta videos de creadores** (streamers, YouTubers) para sacar clips. Hoy esa persona se confunde al abrir la app:

- ve demasiadas casillas de golpe (transcripción, modelo de Whisper, encuadre, fps, tipo de momento, extras…);
- los nombres son técnicos y no del mundo del gaming o el entretenimiento ("Clipping", "Encuadre", "Título gancho", "Transcripción local · small");
- en una pantalla de PC gran parte queda vacía: el formulario es una columna angosta.

El objetivo es que alguien que nunca vio Baitd pegue un link y saque clips sin leer nada más.

## Metas

1. **Estilo nuevo en toda la app** ("C", mezcla de las 3 referencias del usuario):
   - Fondo casi negro (`#0c0c12`) con dos brillos suaves: violeta arriba a la derecha y azul abajo a la izquierda (gradientes radiales, sin imágenes).
   - Paneles de cristal esmerilado: fondo blanco al ~7 %, borde blanco al ~14 %, `backdrop-filter: blur`, esquinas muy redondeadas (~22 px en paneles, 999 px en píldoras y botones).
   - Títulos grandes, gruesos y limpios ("Haz clips."); el resto del texto pequeño y tranquilo, con el gris del texto secundario al ~60 %.
   - Un solo botón que llama la atención por pantalla: blanco con texto oscuro. Los secundarios son contorno.
   - **Cero emojis** en toda la interfaz (pedido explícito). Donde hoy hay emojis o símbolos como iconos (⚙, ⋯, ✕), se cambian por iconos de línea simples en SVG en línea o por texto.
2. **Menú lateral a la izquierda**, en lugar de las pestañas de arriba: **Clips**, **En vivo**, **Short con IA**, y abajo **Ajustes**. Las subpestañas actuales de Clipping ("Video" / "En directo") desaparecen: "Video" es Clips y "En directo" es En vivo.
3. **Pantalla Clips simplificada a fondo** (ver "Pantalla Clips").
4. **Nombres nuevos** (ver tabla), en español y en inglés.

## No metas (esta versión)

- Simplificar a fondo **En vivo** y **Short con IA**: toman el estilo y los nombres nuevos, pero su contenido y orden se quedan como están. Se simplifican en la versión siguiente.
- Funciones nuevas. Todo lo que hace hoy la app sigue igual; solo cambia dónde está y cómo se llama.
- Cambiar los datos guardados: los perfiles, los ajustes y los valores internos (por ejemplo `framing: "partido"`) no cambian.

## Anti-metas (si pasa esto, está mal aunque funcione)

- Que se vea forzado, recargado o "de plantilla": nada de brillos animados, tarjetas con sombras de colores ni gradientes en el texto.
- Perder una opción: cada ajuste que existe hoy tiene que seguir alcanzándose (en Más opciones o en Ajustes).
- Texto ilegible: nada por debajo de 12 px en la app real; el texto normal a 14–15 px.
- Emojis en cualquier parte.

## Nombres

| Ahora | Nuevo (es) | Nuevo (en) |
|---|---|---|
| Clipping | Clips | Clips |
| En directo | En vivo | Live |
| Crear short | Short con IA | AI Short |
| Buscar momentos | Elegir yo los momentos | Pick the moments myself |
| Crear clips | Crear clips | Make clips |
| Tipo de momento: Todos, Gracioso, Interesante, Polémico, Historia | ¿Qué buscas?: Todo, Risas, Épico, Polémica, Historia | What are you after?: Everything, Laughs, Epic, Drama, Story |
| Encuadre: Cara, Centro, Completo, Gameplay | Corte: Sigue la cara, Centro, Todo el video, Gameplay + cámara | Crop: Follow the face, Center, Whole video, Gameplay + cam |
| Extras: Título gancho, Crédito, Música | En el clip: Título arriba, @ del canal, Música | In the clip: Top title, Channel @, Music |
| Transcripción, Modelo, Idioma | dentro de Más opciones → Avanzado | inside More options → Advanced |
| Cola | Tus clips | Your clips |

"Interesante" pasa a llamarse "Épico" solo en pantalla; el valor interno `interesante` no cambia.

## Pantalla Clips

Arriba, de izquierda a derecha y de arriba abajo:

1. **Título** "Haz clips." y debajo, en gris, "Pega un video largo y saca sus mejores momentos."
2. **Un solo cuadro grande** (panel de cristal) con:
   - el campo "Pega el link o suelta tu video aquí…" (acepta link, arrastrar un archivo o "Elegir archivo"; el archivo subido se muestra dentro del cuadro con "Quitar");
   - **3 opciones a la vista**, como píldoras que abren una lista: formato (Vertical 9:16, 4:5, Cuadrado 1:1, Horizontal 16:9), duración (mín–máx en segundos) y cantidad (un número o Todos);
   - el enlace **Más opciones**;
   - los botones **Elegir yo los momentos** (contorno) y **Crear clips →** (blanco).
   - "Añadir chat" (el .json de Twitch) se queda, pero como enlace pequeño dentro de Más opciones, no en el cuadro.
3. **Tus clips**: tarjetas de los clips terminados (miniatura con la forma del formato y el título) que llenan el espacio de abajo. Reemplaza la columna derecha actual (vista previa del teléfono y Cola).
   - Un trabajo en marcha aparece **primero**, como tarjeta ancha: nombre del video, "Paso 2 de 5 · Escuchando el video…", tiempo que falta si se sabe, barra de progreso y **Cancelar**. Un trabajo fallido muestra el error en la misma tarjeta, con **Reintentar** cuando hoy existe.
   - Tocar un clip terminado abre lo que hoy abre "Ver clips".

**Más opciones** es un panel que se abre desde la derecha (encima del contenido, con ✕ dibujado como icono). Dentro, en este orden:

- ¿Qué buscas? (píldoras: Todo, Risas, Épico, Polémica, Historia)
- Corte (píldoras: Sigue la cara, Gameplay + cámara, Centro, Todo el video)
- Subtítulos (estilo y posición)
- En el clip (casillas: Título arriba, @ del canal, Música)
- Chat del directo (Añadir chat)
- Avanzado, cerrado por defecto: idioma, calidad de transcripción (Local con modelo, u OpenAI), 30/60 fps

**Elegir momentos** (después de "Elegir yo los momentos"): ocupa el área principal en lugar del cuadro y de Tus clips. Arriba "N momentos encontrados" y "Marca los que quieras. Toca uno para verlo y moverle el inicio o el final.", con el botón **Exportar N clips →**. Debajo, tarjetas en dos columnas: casilla, miniatura, título, tipo, duración y minuto del video. Tocar una tarjeta abre lo que hoy permite editar un momento (ver el video, inicio, fin, título, corte, crédito, cámara de Gameplay).

## Resto de la app en esta versión

- **En vivo** y **Short con IA**: el mismo marco (menú lateral, fondo, cristal, botones y tipografía nuevos) y los nombres nuevos. Su contenido se queda como está.
- **Ajustes**: abre desde el menú lateral; mismo contenido, con el estilo nuevo.
- **Perfil** ("Por defecto" y su menú ⋯): pasa al pie del menú lateral, encima de Ajustes.
- **Aviso de actualización** y errores generales: se quedan arriba del área principal, con el estilo nuevo.

## Restricciones

- Tecnología actual, sin dependencias nuevas: Flask + Jinja (`src/baitd/web/templates/index.html`), JavaScript sin framework (`static/app.js`), CSS propio (`static/app.css`), textos en `static/i18n/es.json` y `en.json`.
- Funciona sin internet: las fuentes son las que ya trae la app (Inter, Montserrat ExtraBold, Anton, Noto Sans); nada se carga de la web.
- Se conservan los `id` de los controles que usan `app.js` y las pruebas (`tests/test_server.py` comprueba varios, por ejemplo `c-count`, `c-aspect`, `c-fps`).
- Accesibilidad como hoy o mejor: cada control con su etiqueta, el panel de Más opciones se maneja con teclado (Esc cierra, el foco vuelve al botón), contraste suficiente sobre el cristal.
- La app abre en su propia ventana y también en el navegador; tiene que verse bien desde ~1280 px de ancho hasta ventanas angostas.

## Decisiones que tomé yo (revísalas)

- **Solo modo oscuro**: se quita el botón Oscuro/Claro, porque el estilo C es oscuro. Si alguien tenía guardado "claro", se ignora.
- **Ventana angosta** (menos de ~760 px): el menú lateral pasa a una barra arriba con los 3 nombres y un icono de Ajustes; Tus clips en 2 columnas.
- **Iconos**: iconos de línea simples en SVG en línea (menú, cerrar, más), del mismo grosor y color que el texto. Ningún icono de colores.
- **La vista previa del teléfono** de Short con IA se queda en esa pantalla; solo sale de Clips.

## Respuestas a la revisión previa (decisiones mías)

1. **Gameplay + cámara en Más opciones** funciona sin revisar: el perfil ya acepta `framing: "partido"` y el render busca la cámara solo en las caras cuando no hay cuadro (`pipeline.py`, `m.cam is None`); si no la encuentra, corta como "Sigue la cara". Solo hay que añadir la opción al `c-framing`. El cuadro dibujado a mano sigue existiendo en la edición de cada momento.
2. **Tus clips** muestra solo trabajos de Clips y En vivo (`clip_auto`, `clip_analyze`, `clip_export`, `live_title`), no los de Short con IA. **Short con IA conserva su columna actual** (vista previa del teléfono y su Cola). Si no hay miniatura, la tarjeta es un color plano con el título; no se crea un endpoint nuevo para miniaturas en esta versión.
3. **Tarjeta de progreso**: usa los pasos que ya manda el servidor (`CLIP_STEPS` y sus textos `step.*`), con textos más amables en i18n si hace falta. El tiempo que falta se muestra solo si el servidor ya lo da. Reintentar sigue la regla de hoy (`RETRYABLE`: error y cancelado).
4. **Controles movidos**: todos los `id` se conservan. Cantidad es una píldora que abre un pequeño panel con `c-count` y `c-count-all`. Duración abre `c-min`/`c-max` con su validación actual. Las ayudas "?" se quedan dentro de esos paneles. En Subtítulos van todos los controles de hoy (estilo, posición, tamaño, color, resaltado). Los resúmenes `sum-*` de los acordeones se pueden quitar si nada más los lee.
5. **Elegir momentos**: la edición de un momento se abre en el mismo panel lateral derecho que Más opciones (más ancho), con el video, inicio, fin, título, corte, crédito y cuadro de cámara de hoy. Se conservan todos los controles actuales de la revisión (descartar, nota, por qué, fuente), en la cabecera o al pie, con el estilo nuevo.
6. **Avisos**: el aviso de derechos va en pequeño debajo del cuadro grande; `#clip-status` y `#clip-error` justo debajo del cuadro. La fila de chat completa (`c-chat-btn`, `c-chat-hint`, el archivo y el chip) pasa a Más opciones con sus `id`.
7. **Tema**: se quitan los botones Oscuro/Claro, el ajuste `s-theme` y el CSS del tema claro. Un valor guardado "light" se ignora.
8. **Pruebas**: se pueden actualizar las que comprueban `id` o textos que cambian a propósito (pestañas `tab-*`, subpestañas, claves de i18n), siempre que sigan comprobando lo mismo. No se dejan `id` viejos escondidos solo para las pruebas.

## Lo que queda a quien lo construya

- Valores exactos de espaciado, tamaños y animaciones (las justas: abrir y cerrar el panel).
- Cómo se dibujan las miniaturas de Tus clips (primer fotograma o color plano si no hay).
- El orden de los cambios y cómo dividir el trabajo en pasos.
