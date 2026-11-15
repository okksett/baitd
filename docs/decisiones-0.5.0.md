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

10. **Algunos canales grandes de Twitch usan fMP4.** illojuan entrega listas con `EXT-X-MAP` en todas las calidades (h264 de 160p a 1080p60), así que la captura se detiene con "Este directo usa un formato que Baitd aún no lee". La regla R10 suponía que ninguna plataforma usaba fMP4. Otros 4 canales de Twitch probados (baitybait, rickyedit, juniorhealy, orslok) seguían en MPEG-TS. Si no se atiende: esos canales no se pueden capturar.
