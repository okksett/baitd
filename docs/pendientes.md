# Baitd: pendientes

Lista de cambios pedidos que todavía no se hacen. Cuando se haga uno, se borra de aquí.

## Urgente

0. **Cambiar el modelo de Gemini por defecto.** Hoy es `gemini-2.5-flash` (`src/baitd/llm/__init__.py`, `DEFAULT_MODELS`). Con una clave nueva da "no existe", porque Google limita los modelos 2.5 a cuentas que ya los usaban. Además, ese modelo se apaga el 1 de diciembre de 2026. Cambiarlo a `gemini-3.6-flash`, el Flash estable según aistudio.google.com/docs/models, consultado el 2026-11-09. Mientras tanto, cada persona tiene que escribirlo a mano en el perfil.
0b. **Sacar el modelo de IA a una casilla propia, visible, para elegirlo.** Hoy está escondido en Crear short → Guion como texto libre, y Clipping lo usa sin mostrarlo. Cuando falla, el error dice "Cambia el modelo en el perfil" y no se entiende dónde. Lo que se pidió:
   - Una casilla "Modelo de IA" a la vista, fuera de la sección Guion, que sirva para Crear short y para Clipping.
   - Una lista desplegable para elegir el modelo, en vez de escribir el nombre a mano.
   - Que la persona pueda elegir el modelo que quiera.

   Propuesta, a confirmar al implementar:
   - Llenar la lista con los modelos que la clave de cada persona puede usar, pidiéndoselos a Gemini u OpenAI con un botón "Actualizar lista". Así nunca vuelve a quedar un modelo que "no existe".
   - Dejar la opción "Otro…" para escribir uno a mano, por ejemplo para Ollama.
   - **Decidido (2026-11-09): va en ⚙ Ajustes, junto a las claves.** Vale para todos los perfiles y para las dos pestañas. Hay que quitar el campo "Modelo" de Crear short → Guion. Los perfiles que ya tengan un modelo escrito deben migrar ese valor a Ajustes una sola vez. El mensaje de error debe decir "Cambia el modelo en Ajustes".

0c. **Reintentar solo cuando la IA está saturada.** Gemini respondió 503 (servidores saturados) y el trabajo falló de inmediato. Hoy no se reintenta nada (`src/baitd/llm/_http.py`). Reintentar 2 o 3 veces con espera creciente, unos 5, 15 y 30 s, cuando la respuesta sea 503 o 500, y quizás 429.
0d. **Botón "Reintentar" en Clipping que no repita lo ya hecho.** Si falla el paso de momentos con IA, hoy hay que quitar el trabajo y lanzarlo de nuevo, y eso vuelve a descargar y a transcribir el video (varios minutos). Reintentar debería seguir desde el paso que falló, conservando el video y la transcripción.

## Interfaz de Clipping (próxima versión)

1. **"Clips" necesita una explicación.** Agregar un **?** junto a "Clips" que diga qué es: cuántos clips sacar del video.
2. **"Duración · s" debe mostrar mínimo y máximo.** Las dos casillas (30 y 60) no dicen cuál es cuál. Poner "mín" y "máx" visibles. Hoy solo lo saben los lectores de pantalla.
3. **El paso 1 parece congelado.** La descarga solo llena del 0 % al 20 % de la barra, y no dice cuánto falta. Mostrar el porcentaje o los MB descargados en el texto ("Preparando el video… 45 %").
4. **La descarga es en 4K.** Un video de 25 min pesa ~1 GB. Se eligió así para que el recorte vertical salga nítido, pero con una conexión lenta tarda mucho. Decidir si bajar a 1080p o dejarlo como opción.

## Listo pero sin publicar

5. **Sesión de YouTube (versión 0.2.2).** Está en la rama `feat/youtube-cookies` (commit `c572003`), solo en este PC. Agrega en Ajustes → Clipping el navegador o un archivo cookies.txt para pasar el bloqueo de "confirma que no eres un bot". Falta subirla, compilar y publicar la Release.

## Más adelante

6. **Instalador `Baitd-Setup.exe`.** Un solo archivo que instala Baitd con icono en el escritorio y en el menú Inicio, y desinstalador. Unas 2 h. Opcional después: que se abra en su propia ventana en vez del navegador (unas 3 h más).
7. **Aviso de puerto ocupado.** Si se abre Baitd con `--port` en un puerto que otro programa ya usa, en Windows no avisa y la página que se abre es la del otro programa. Solo afecta a quien escribe `--port` a mano.

## Sin probar todavía

8. Clipping completo (pasos 3 a 5: momentos con IA, encuadre y render) con un video real y una clave de Gemini.
9. Las apps de Mac y Linux en una máquina real.
10. Pasar un bloqueo real de YouTube usando cookies reales (opción del punto 5).
