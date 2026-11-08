# Baitd Clipping: decisiones que tomé yo

Durante la construcción de Clipping tuve que decidir cosas que ni el diseño ni el plan dejaban cerradas. Cada línea dice qué decidí y qué cuesta si estuvo mal. Todas se pueden cambiar.

## Las que más pueden afectarte

| # | Decisión | Si estuvo mal |
|---|---|---|
| P5 | Clipping usa el **formato y los fps del perfil**, los mismos que "Crear short". Si cambias el formato en una pestaña, cambia en la otra. | Si quieres formatos distintos para shorts y clips, hay que separarlos. |
| T8c, T9a | El **video original se borra** cuando ya nadie lo necesita: después de exportar, al descartar el análisis o cuando falla un trabajo. Nunca se borra si otro trabajo de la cola lo usa. "Guardar videos originales" en Ajustes lo evita. | Si no activas esa opción, después de exportar ya no puedes volver a esos candidatos. |
| FW4 | Si escribes un **crédito en una tarjeta**, se muestra aunque el interruptor de crédito esté apagado. | Se cambia en una línea de `clip/render.py`. |
| T9c | Al exportar, cada clip puede durar de **10 a 180 s**, aunque tu perfil diga 30–60. La lista de revisión te deja alargarlo. | Un clip puede salir más largo de lo que pide el perfil. |
| T7d | Un video de 24 o 25 fps se exporta a **30 fps**, duplicando cuadros. Solo se exporta a 30 o 60. | Algún clip de cine puede verse un poco menos fluido. |
| A2 | Al abrir la app se **borran los análisis y los videos que ningún trabajo usa**. También se borran las subidas que quedaron a medias. | Un análisis cuyo trabajo quitaste de la cola ya no se puede abrir. |
| B5 | El crédito usa el **@ del canal** cuando el link lo trae. Si no, pone el nombre del canal sin "@". | Algunos créditos salen sin @. |

## Seguridad

| # | Decisión | Si estuvo mal |
|---|---|---|
| FW2 | Los videos se suben en **partes de 64 MB**. La única excepción a "solo JSON" es exactamente `PUT /api/uploads/<id>`, con un encabezado especial. El tipo de archivo, el tamaño y el espacio en disco se revisan **antes** de guardar el primer byte. Una web externa ya no puede llenarte el disco con un envío de 40 GB. | Ninguno conocido. Como mucho, una web podría hacer que se escriban 257 MB temporales, que se borran solos. |
| T3b | Solo se aceptan **MP4, MOV, MKV, WEBM, MP3 y WAV**, y los identificadores de archivo son siempre 12 caracteres hexadecimales. Así nadie puede escapar de la carpeta. | Un formato raro como `.ts` se rechaza. |
| T3c | "Actualizar yt-dlp" verifica la firma SHA-256 oficial antes de reemplazarlo. | Ninguno. |
| T9d | El servidor nunca devuelve la ruta local de tus archivos. | Ninguno. |

## Construcción

| # | Decisión | Si estuvo mal |
|---|---|---|
| T6c | El modelo de detección de caras (YuNet) se bajó del repositorio oficial de OpenCV en Hugging Face, porque GitHub fallaba desde aquí. El hash coincide exacto con el oficial. | Ninguno: es el mismo archivo. |
| T11 | El zip de Windows pesa **191.9 MB**. CI falla si cualquier sistema pasa de 450 MB. No se incluye ningún modelo Whisper: se baja la primera vez que transcribes localmente (~250 MB). | La primera transcripción local tarda más. |
| B1 | `--selftest` ahora también prueba la detección de caras, la transcripción y yt-dlp, sin internet. | Ninguno. |
| T3d | Cada commit nombra el modelo que lo escribió. | Ninguno. |

## Detalles técnicos internos

| # | Decisión | Si estuvo mal |
|---|---|---|
| T4b, P4 | La transcripción local va en bloques de 10 min, para no cargar 4 h en memoria y poder cancelar. | Una frase cortada justo en el borde de un bloque. |
| T5a | Una frase termina en punto o en una pausa de 0.8 s o más. | Algún clip empieza o termina un poco antes o después de lo ideal. |
| T5b | La "energía" de un momento sale del volumen percibido y suma hasta 15 puntos. No detecta risas por sí sola. | Los momentos más ruidosos pesan un poco más. |
| T6a, T7a | El encuadre se calcula sobre el clip antes de quitar silencios y luego se ajusta a los cortes. | Ninguno conocido. |
| T7b, T7c | El audio se arma en dos pasos y se quitan silencios largos dejando 0.15 s. | Un paso extra de FFmpeg por clip. |
| P1–P3 | Detalles internos de cómo se organizan los módulos y las claves guardadas en el navegador. | Ninguno conocido. |

## Lo que dejé para una versión siguiente

- Los clips terminados no se pueden ver dentro de la app; se abren desde su carpeta. La lista de candidatos está a la izquierda, no a la derecha como en el diseño.
- No hay aviso cuando un MKV o un video HEVC no se puede previsualizar en el navegador.
- Cancelar mientras yt-dlp descarga o une un video largo puede tardar minutos.
- Actualizar yt-dlp con la app abierta: conviene reiniciarla después.
- Los trabajos de clips que fallan no tienen botón "Reintentar"; hay que volver a lanzarlos.
- Mientras se sube un video, el resto de la app queda bloqueado.
- "Descargando modelo…" aparece un instante aunque el modelo ya esté bajado.

## Lo que nadie ha probado todavía

1. Las apps de Mac y Linux, y su tamaño. Hay que subir el repositorio a GitHub para que se compilen.
2. Un video real de 1 hora con Whisper local y con OpenAI, midiendo si cumple los 45 y 15 min del diseño.
3. Links reales de YouTube o Twitch (las pruebas no usan internet).
4. Que la detección de caras siga bien a la persona que habla en un podcast real.
