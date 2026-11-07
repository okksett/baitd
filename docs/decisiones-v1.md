# Baitd v1: decisiones que tomé yo

Durante la construcción tuve que decidir cosas que ni el diseño ni el plan dejaban cerradas. Cada línea dice qué decidí y qué cuesta si estuvo mal. Todas se pueden cambiar.

## Las que más pueden afectarte

| # | Decisión | Si estuvo mal |
|---|---|---|
| R28 | El **gancho** es siempre la primera frase de la escena 1. En el modo revisión ya no hay un campo "Gancho" aparte: se edita la escena 1. | No se puede tener un gancho escrito distinto de lo que se dice primero. |
| R29 | El gancho tiene **8 palabras como máximo**. Si la IA escribe uno de más de 10, se le pide que lo corrija. Si vuelve a fallar, se acepta igual y no se cancela el video. | De vez en cuando sale un gancho de unos 4 s. |
| R27 | El **desvanecido final** se aplica solo a la música. La voz se oye completa hasta la última palabra. | El cierre puede sentirse un poco seco. Se arregla añadiendo medio segundo de cola. |
| R20, R21 | Música incluida: **1 pista libre (CC0) por ambiente**, sacada de FreePD a través de Internet Archive. | Poca variedad hasta que agregues tu propia música. Si la licencia de alguna pista no fuera CC0, se reemplaza esa pista. |
| R30 | La carpeta de clips descargados tiene un **tope de 3 GB**. Lo más viejo se borra solo. | Algún clip se vuelve a descargar. |
| R32 | El "primer clip más llamativo" se elige por orientación y duración, no viendo la imagen. | El primer plano puede ser menos impactante de lo ideal. |
| R25 | En Windows y Linux, la app abre una **ventana de consola** (muestra la dirección y se cierra con ella). En Mac no hay consola, así que se cierra con **⚙ → Salir de Baitd**. | Hay una ventana negra extra en Windows. |
| R23 | Tus datos quedan en `%LOCALAPPDATA%\Baitd\Baitd` (el nombre sale dos veces). | Solo es una ruta rara, no afecta nada. |
| R10 | Modelos por defecto: gpt-4o-mini (OpenAI), gemini-2.5-flash (Gemini), llama3.1 (Ollama). | Si un modelo deja de existir, se cambia en el perfil. |

## Seguridad y privacidad

| # | Decisión | Si estuvo mal |
|---|---|---|
| R22 | El servidor local solo acepta órdenes en JSON que vengan de `127.0.0.1` o `localhost`. Así ninguna página web puede manejarlo ni robar tus claves. | Ninguno conocido. |
| R13 | La clave de Pixabay va en la URL porque Pixabay lo exige, pero esas URL nunca se guardan en el registro ni aparecen en los errores. | Ninguno. |

## Construcción y entrega

| # | Decisión | Si estuvo mal |
|---|---|---|
| R6 | **No subí nada a GitHub.** La compilación para Mac y Linux está escrita, pero nunca corrió. | Mac y Linux siguen sin probar hasta que la subas. |
| R26 | El Mac Intel compila en `macos-15-intel`, porque GitHub retiró `macos-13`. | Se cambia una línea. |
| R24 | `--selftest` arma un video de prueba sin internet para comprobar que la app trae todo. | Ninguno. |
| R33 | "Salir" también cierra las conexiones abiertas del navegador, y si eso falla, cierra igual de forma normal. waitress quedó fijado a la versión 3. | Hay que revisarlo si se actualiza waitress a la 4. |

## Detalles técnicos internos

| # | Decisión | Si estuvo mal |
|---|---|---|
| R1–R4, R8, R11 | Detalles de cómo se conectan FFmpeg, el progreso, los cortes, las fuentes y las palabras de los subtítulos entre módulos. | Hay que tocar algunas firmas de funciones. |
| R3, R14, R15 | Planos de 2 s como mínimo; el plano del gancho dura entre 1 y 2.5 s; una escena muy corta se une a la siguiente. | Pequeñas diferencias de ritmo. |
| R7 | La cantidad de palabras es segundos × 2.5 (45 s dan unas 112 palabras). | Guiones un poco largos o cortos. |
| R12 | Cancelar espera a que termine el paso en curso, unos segundos. | Cancelar tarda un poco en reaccionar. |
| R16 | Un clip no se repite dentro del mismo video. | Ninguno. |
| R17–R19 | Cómo se dibujan los subtítulos: dos capas en "caja azul", gancho más grande y palabra activa sin parpadeo. | Ajustes visuales. |
| R5, R9, R18 | Referencia visual, saltos de línea de los archivos y copia de fuentes. | Ninguno. |

## Lo que dejé para una versión siguiente (R31, R34)

- Los mensajes de error del servidor siguen en español aunque la interfaz esté en inglés.
- No se puede elegir otra fuente de subtítulos ni otro contorno; se usan los estilos predefinidos.
- No hay transición suave entre clips.
- Cancelar mientras se descargan clips puede tardar.
- Casos raros de cierre que pueden dejar una carpeta de más en `salidas/`.

## Lo que nadie ha probado todavía

1. Las apps de Mac y Linux. Hay que subir el repositorio a GitHub para que se compilen.
2. Videos reales con claves reales de OpenAI, Gemini, ElevenLabs, Pexels y Pixabay.
3. Si cada música encaja con su ambiente. Nadie la ha escuchado.
4. Cerrar la ventana de consola con la ✕ en Windows en medio de un video.
