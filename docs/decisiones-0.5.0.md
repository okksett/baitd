# Baitd 0.5.0: decisiones

Qué se decidió en esta versión ("Clipping en directo"), por qué, y qué cuesta si resulta ser un error.

## Detección automática en canales de otros (§6.4)

1. **Las señales del audio son solo subidas de volumen.** Las risas que se oyen en el directo las juzga la IA cuando lee la transcripción del tramo. Detectar risas en el sonido pediría un modelo más, corriendo todo el tiempo en la PC del stream. Si se equivocó: un momento gracioso con el chat callado y sin gritos solo se encuentra si alguien lo marca a mano.
2. **Para transcribir y preguntar a la IA se usa el primer perfil guardado.** El servidor no sabe qué perfil tiene elegido la ventana, y la detección corre aunque nadie la mire. Del perfil salen el transcriptor (local o API), el modelo y el idioma. Si se equivocó: quien tenga varios perfiles tiene que poner primero el que quiere para el directo.
3. **La transcripción local del directo usa 2 hilos y queda en memoria.** Ese modelo se carga una vez y se reutiliza mientras Baitd está abierto, para no cargarlo en cada candidato. La exportación sigue cargando su propio modelo y soltándolo al terminar, como antes. Si se equivocó: el modelo del directo ocupa memoria hasta cerrar Baitd (unos 250 MB con "small"), y mientras se exporta hay dos copias.
4. **Al detener la captura no se pierden candidatos.** Lo que estaba abierto o esperando, incluido el que se estaba transcribiendo, se guarda sin clasificar, sin transcribir ni pedir a la IA. Si se equivocó: aparecen en la lista momentos sin título de IA, que se pueden borrar.
5. **Un candidato que junta señales distintas muestra el motivo más claro:** primero "risas", luego "el chat explotó" y al final "volumen". Los candidatos que se pisan se juntan, hasta 2 minutos; uno que se alargaría más empieza donde termina el anterior. Si se equivocó: el motivo mostrado es menos exacto, nada más.
6. **Esperan como mucho 3 candidatos; uno nuevo deja fuera al más viejo.** Además, el búfer nunca queda retenido más de 7 minutos por detrás de lo último recibido (5 del búfer más 2 de un candidato), aunque un candidato se atasque transcribiendo o esperando a la IA. Si el video de ese candidato ya se borró, se descarta, o se guarda con lo que quede. Si se equivocó: en una PC muy lenta y una racha muy movida se pierden algunos candidatos. Siempre se puede marcar a mano.

## Calibración (2026-11-15)

Se capturaron 10 minutos de 4 canales en vivo con `tools/live_calibrate.py`, que no transcribe ni llama a la IA. Se eligieron dos canales por plataforma, uno con chat grande y otro con chat pequeño:

| Canal | Espectadores | Mensajes por minuto | Mediana por tramo de 10 s | Risas |
|---|---|---|---|---|
| kick/lacobraaa (un partido) | ~93 000 | 798 | 119 | 5 % |
| kick/elcesarlive | ~240 | 7,8 | 1 | 11 % |
| twitch/baitybait | ~6 000 | 61 | 7,5 | 21 % |
| twitch/elgamerwicho | ~250 | 4,8 | 0 | 7 % |

El directo de lacobraaa terminó a los 7,8 minutos. El primer canal grande elegido en Twitch, illojuan (~48 000 espectadores), no se pudo capturar: ver la decisión 10.

**Valores finales** (se pueden volver a probar con `--replay` sobre los registros guardados):

| Constante | Al empezar | Final | Por qué |
|---|---|---|---|
| `BIN_S` | 10 | 10 | |
| `BASELINE_S` | 300 | 300 | |
| `MIN_HISTORY_S` | 60 | 60 | |
| `CHAT_RATE_X` | 3,0 | **2,0** | En un chat grande el ritmo varía poco: el tramo más alto de lacobraaa fue 2,04 veces la mediana, así que con 3 nunca habría saltado. |
| `CHAT_MIN_MSGS` | 8 | **10** | Compensa el 2,0 en los chats pequeños: el tramo más alto de elcesarlive tuvo 9 mensajes (9 veces su mediana de 1), y eso es ruido. |
| `LAUGH_SHARE` | 0,35 | 0,35 | |
| `LAUGH_MIN` | 5 | 5 | |
| `LAUGH_RATE_X` | — | **4,0 (nueva)** | Un chat grande nunca llega al 35 % de risas. Ahora también cuenta como risa un tramo con 4 veces más risas que las habituales en ese chat (lacobraaa pasó de 5 a 30). |
| `LOUD_LU` | 8,0 | **10,0** | Con 8, el canal pequeño de Kick daba 5 picos de volumen en 10 minutos, casi todos de 8 a 9 LU. |
| `COOLDOWN_S` | 45 | 45 | |

**Picos medidos** en los mismos 10 minutos (chat / risas / volumen):

| Canal | Al empezar | Final |
|---|---|---|
| kick/lacobraaa (grande, 7,8 min) | 0 / 0 / 0 = 0 | 1 / 1 / 0 = **2** |
| kick/elcesarlive (pequeño) | 1 / 0 / 5 = 6 | 0 / 0 / 2 = **2** |
| twitch/baitybait (grande) | 0 / 3 / 0 = 3 | 3 / 3 / 0 = **6** |
| twitch/elgamerwicho (pequeño) | 0 / 0 / 0 = 0 | 0 / 0 / 0 = **0** |

El objetivo era de 2 a 8 picos por cada 10 minutos en un chat movido y de 0 a 2 en uno tranquilo. Los cuatro canales quedan dentro. Si se equivocó: con muchos picos se gastan antes las 6 peticiones por hora a la IA y llegan más momentos sin clasificar; con pocos se escapan momentos, y queda la tecla.

7. **Medir el volumen cuesta poco.** Cada trozo de video se mide con un FFmpeg de prioridad baja, en un hilo propio y nunca en el de la captura. Medir un trozo de 2 s tardó una mediana de 0,14 a 0,22 s, con cuatro capturas y la batería de pruebas corriendo a la vez. Si se equivocó: en una PC muy justa hay que medir uno de cada dos trozos.
8. **Los trozos de Kick ahora duran 2 s**, y no los 4,17 s medidos antes. La captura no depende de esa duración.
9. **Los mensajes del chat se cuentan con el reloj de la PC.** El momento del pico se lleva al tiempo del video restando lo que va de la última imagen recibida a ahora. El público reacciona a lo que ve con su propio retraso, que es parecido al nuestro. Si se equivocó: el tramo candidato queda corrido unos pocos segundos, dentro de los 40 s de margen anteriores.

## Hallazgos para revisar

10. **Algunos canales grandes de Twitch usan fMP4.** illojuan entrega listas con `EXT-X-MAP` en todas las calidades (h264 de 160p a 1080p60), así que la captura se detiene con "Este directo usa un formato que Baitd aún no lee". La regla R10 suponía que ninguna plataforma usaba fMP4. Otros 4 canales de Twitch probados (baitybait, rickyedit, juniorhealy, orslok) seguían en MPEG-TS. Si no se atiende: esos canales no se pueden capturar. **Resuelto más abajo (decisión 11): la regla R10 se revirtió y fMP4 ya se captura.**


## Captura y reloj

11. **Se revirtió la regla R10: fMP4 sí se captura.** La regla original era "no leer listas con `EXT-X-MAP`", porque se creía que ninguna plataforma las usaba. Pero illojuan en Twitch las usa en todas las calidades, y Twitch está en la lista de plataformas del diseño. Ahora cada fragmento se guarda con su trozo de inicio delante, y al cortar se unen los fragmentos seguidos que tienen el mismo inicio, y se cierra el hueco entre tandas, igual que con MPEG-TS. Medido en Twitch illojuan. Si se equivocó: un directo fMP4 con un salto de tiempo y el mismo inicio se une sin marcar el salto (pendiente menor).
12. **El reloj de la captura sale de la cabecera HTTP `Date` de la lista.** Es la mediana de las últimas 5 respuestas, más 0,5 s porque la cabecera trunca al segundo. El `X-SERVER-TIME` de la lista solo se usa si avanzó desde la vez anterior: en la práctica se quedaba fijo y congelaba el reloj (la latencia caía un segundo por segundo, hasta -107 s). Además, el reloj de la PC donde se probó iba unos 11 s atrasado respecto a la hora real, así que el reloj local solo no sirve. Latencia medida en Kick con este arreglo: de 3,3 a 3,8 s, estable. Si se equivocó: las marcas del canal propio se corren como mucho medio segundo.
13. **Sin `EXT-X-PROGRAM-DATE-TIME`, el último trozo de la primera tanda se ancla a "ahora menos el retraso".** La fórmula del plan dejaba los trozos posteriores de esa primera tanda en el futuro. Hoy ninguna de las 3 plataformas carece de esas marcas. Si se equivocó: las marcas a mano en un directo sin marcas de tiempo se corren lo que dura la primera tanda.
14. **Al empezar solo se bajan los últimos 60 s (`FIRST_FETCH_S`), no todo el búfer** (R3), para no tragar más de 200 MB de golpe en la PC del stream. En el canal propio "ahora" es la hora del servidor; en los de otros, el final del búfer (R5). `delay_s` solo se usa si la lista no trae marcas de tiempo (por defecto 8 s en Kick, 6 en Twitch y 15 en YouTube).
15. **El video de un momento guarda la marca y hasta 20 s por cada lado** (R2, lo que tenga el búfer), para poder ampliarlo al revisar.

## Marcas, teclas y momentos guardados

16. **Un momento es una fuente y un análisis** con `"origin": "live"` y un bloque `"live"` (R1). "Revisar" abre la tarjeta de siempre. Exportar varios momentos pone en la cola un `clip_export` por cada uno. Un momento manual o sin clasificar es de tipo `"interesante"` y su tarjeta muestra el motivo ("Lo marcaste tú", "El chat explotó"…) en vez de una puntuación (R9).
17. **Una pulsación nueva después del final de la marca abierta empieza otro momento.** La marca vieja pasa a una cola y sale cuando el búfer llega a su final; mientras el directo vaya con retraso se queda pendiente. El plan no decía nada y el diseño (§6.3) pide un momento nuevo. Si se equivocó: retoque pequeño en la sesión.
18. **La tecla se registra mientras Baitd está abierto y haya al menos un canal guardado** (R4), para poder explicar en la ventana una pulsación sin captura. Se suelta al quitar el último canal y al salir. En Windows se usa `MOD_NOREPEAT`: mantener pulsada la tecla cuenta una vez. Letras y números necesitan Ctrl, Alt o Win; Shift solo no vale (Mayús+A robaría la A mayúscula en todo el sistema). Las teclas F y pausa, insertar, inicio, fin, re pág y av pág pueden ir solas. Si se equivocó: una combinación rechazada que el usuario puede cambiar.
19. **Los avisos de "sin captura" y "tecla ocupada" salen en toda la app**, no solo en el panel En directo (el diseño §6.1 pide que la ventana lo explique). Las tarjetas muestran `length` (fin menos inicio del momento) y no la duración del archivo con el margen.
20. **Un momento solo se va cuando el usuario lo quita o una exportación correcta lo libera** (§6.5). Quitar de la cola un trabajo fallido o terminado nunca borra un momento en directo ni su video. Una exportación correcta siempre quita el momento de la lista, aunque "guardar videos originales" esté activo; el video fuente sí sigue esa opción. Si se equivocó: quien guarda originales no puede volver a exportar el mismo momento desde la lista.
21. **Los canales de YouTube con identificador `UC…` se acreditan con el nombre visible**, o con el identificador a secas si no hay nombre, para no escribir "@UCxxxx". El crédito sobre clips de otros siempre se muestra, y la insignia de plataforma sale del análisis, nunca del navegador (R8). Si se equivocó: un crédito raro que se edita al revisar.
22. **"partido" entra en la lista de encuadres desde el principio**, y hasta que existió el recorte se trataba como "cara". Así los análisis guardados antes no dejaban de cargar.

## Exportación y encuadres

23. **Exportar un momento en directo corre todo FFmpeg con prioridad baja** (diseño §4), también la extracción de audio para el título de IA. La transcripción de Whisper al exportar sigue con prioridad normal. Si se equivocó: exportar tarda más, o la PC del stream tiembla.
24. **Los hilos propios de CTranslate2 quedan en prioridad normal.** La transcripción en directo corre en un hilo de prioridad baja con 2 hilos de cálculo, pero los hilos de inferencia que crea la biblioteca por dentro no se bajan de prioridad. Si se equivocó: con un juego exigente, transcribir un candidato puede restarle algo de CPU al juego.
25. **El título de IA es por momento y a petición.** Los momentos marcados a mano no gastan IA ni transcripción durante la captura; el título se pide en la revisión (un trabajo `live_title`) y la transcripción se hace al exportar. Si se equivocó: hay que pulsar el botón en cada momento. El servidor no evita un doble clic: gasta dos peticiones de IA.
26. **Encuadre "partido"** (R7): el recorte del juego es el centro del fotograma, sin intentar adivinar "el centro de la acción". Respeta el límite de ampliación solo para ese recorte: con una fuente de menos de unos 640 px de alto, pasa a "completo". El recorte de la cámara no tiene ese límite, no se afila la imagen y una cámara puesta por el usuario se escala directa a la banda de 1080 x 768, así que el editor bloquea esa proporción. Si se equivocó: una cámara estirada si la interfaz deja cambiar la proporción.
27. **El crédito sobre clips de otros dibuja la insignia con una capa de color por línea de diálogo**, con burbuja sólida en Twitch y borde oscuro de 2 px en los logos. Se aceptó tras ver los fotogramas. Si se equivocó: es solo estético.

## Chat

28. **El chat de Kick pasó de Pusher a Centrifugo (hallazgo del 2026-11-15).** La clave antigua de Pusher da error 4001. Ahora se pide un token de invitado (`POST web.kick.com/api/v1/realtime/auth/connection`, dura unos 45 min), se conecta por websocket a `realtime.us-east-1.platform.kick.com` con `Origin: https://kick.com`, y se suscribe a `chatrooms.<id>.v2`. Los mensajes llegan en `ChatMessageEvent`, y hay que responder `{}` a los `{}` de ping. Probado: 6 mensajes en 30 s en un canal de 1 261 espectadores. Si Kick lo vuelve a cambiar: el chat de Kick avisa de que no hay chat y el detector sigue con el audio. Pendiente menor: la dirección del websocket está escrita a mano en vez de pedirla a `/api/v1/realtime/connection`.
29. **El chat de YouTube necesita un User-Agent de un Chrome actual.** Con un "Mozilla/5.0" a secas, YouTube contesta "versión antigua del navegador". Si se equivocó: cuando ese User-Agent quede viejo habrá que subirlo.
30. **Todo se lee como espectador anónimo**: sin cuentas ni inicio de sesión, y los tokens y las direcciones de las listas nunca salen en registros ni errores (solo `esquema://servidor/…`).

## Lo que no se alcanzó

31. **Risas en el sonido: no se detectan** (R6). Cuando el tramo llega a la IA, ella juzga las risas por la transcripción. Las risas del chat sí cuentan (`LAUGH_SHARE`, `LAUGH_RATE_X`).
32. **El perfil que se usa en la detección automática es el primero guardado** (ver decisión 2). Elegir un perfil por directo queda para otra versión.
33. **Sin comprobar en directo todavía:** el campo de retraso y los avisos de tecla ocupada, chat apagado y disco lleno.
34. **Pendientes menores conocidos** (anotados al revisar cada tarea, ninguno bloquea): el sondeo del estado va a 1 Hz con la ventana oculta; un candidato que falla por un error inesperado se descarta en vez de guardarse sin clasificar; hay un solo cerrojo para cargar modelos; `live_ai_per_hour` se lee una vez por sesión; una pulsación sin captura puede tardar hasta 10 s en mostrar su aviso.

## Empaquetado y autoprueba

35. **`aiohttp` y `pynput` van en `pyproject.toml`** (`pynput` solo fuera de Windows, donde la tecla usa `user32`), y `baitd.spec` recoge los submódulos de `aiohttp` siempre y los de `pynput` fuera de Windows, porque se importan según el sistema y PyInstaller no los ve solo.
36. **La autoprueba (`--selftest`) comprueba ahora el camino en directo, sin red y sin registrar ninguna tecla.** Genera 4 trozos MPEG-TS de 2 s con una lista con marcas de tiempo, la lee, los mete en el búfer y corta de 2 a 6 s (debe salir un MP4 con video de unos 8 s). Repite con dos fragmentos fMP4 (inicio + fragmento). Comprueba `import aiohttp`. En Windows comprueba que `hotkey.parse_hotkey("F9")` funciona y que `RegisterHotKey` existe; en Mac y Linux, solo lo de `parse_hotkey`. Dura un segundo. Si se olvida: una app empaquetada sin `aiohttp` o sin el búfer fallaría en casa del usuario y no en CI.
