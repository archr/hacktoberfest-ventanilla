# Demo en Docker y Coolify

`compose.yaml` contiene dos servicios sin puertos publicados en el host:

- `app`: chat en 8700, con el volumen persistente `app-data` para índice y documentos.
- `documents`: generador simulado privado en 8701. Devuelve el PDF inline para evitar
  guardar una segunda copia. No se le asigna dominio.

Ambos usan Python 3.14 y un usuario sin privilegios. La imagen excluye `.env`, el índice,
documentos generados, credenciales y archivos ajenos al código de la app.

## Coolify

1. Crea una aplicación desde el repositorio público, rama `main`, build pack **Docker
   Compose**, archivo `/compose.yaml`.
2. Configura `LLM_BASE_URL` con la dirección de Ollama accesible **desde Docker**, incluyendo
   `/v1`; `localhost` apuntaría al propio contenedor. Usa una red privada como Tailscale.
   Ajusta `LLM_MODEL`, `EMBED_MODEL` y, si corresponde, `EMBED_BASE_URL` / `LLM_API_KEY`.
   Estos valores se guardan en Coolify, nunca en el repositorio ni durante el build.
3. Asigna el dominio solo al servicio `app`, con puerto interno 8700, por ejemplo
   `https://ventanilla.example.org:8700` (el puerto selecciona el destino interno del proxy).
   Si Cloudflare ya termina HTTPS y conecta al origen por HTTP, usa
   `http://ventanilla.example.org:8700` y desactiva el redirect a HTTPS en Coolify para evitar
   un bucle. La URL pública sigue siendo HTTPS. Sigue la configuración de tu origen.
4. Despliega. Si el volumen no contiene fragmentos, el entrypoint ejecuta la ingesta
   antes de arrancar. La primera vez necesita acceso a las fuentes y a embeddings.
   El healthcheck permite hasta diez minutos para ello.
5. Verifica `/healthz` (proceso), `/api/health` (modelo, índice y vectores), una pregunta con
   citas y la generación/descarga de un documento con datos ficticios.

La app continúa gestionada por Coolify aunque el workspace de desarrollo deje de existir.
No expongas Ollama ni el servicio de documentos a Internet.

## Usar un índice ya construido

Con el mismo modelo de embeddings, puedes copiar **solo** `index.sqlite` al volumen antes
del primer arranque. Crea una copia consistente con la API de backup de SQLite; no copies
el archivo de una base abierta directamente. No migres documentos personales de pruebas.
El directorio del volumen debe permitir escritura al uid/gid 10001.

La ingesta omite el trabajo si ya hay fragmentos. Para actualizar las fuentes, detén el
servicio `app`, ejecuta `python -m app.rag.ingest --refresh` en un contenedor con el mismo
volumen y configuración, y vuelve a arrancar. Reiniciar es obligatorio porque los vectores
se cargan en memoria. Al cambiar el modelo de embeddings, reconstruye todo el índice.

## Límites de la demo

- Una sola instancia/worker: máximo 2 POST simultáneos y 30 POST por minuto entre todos los
  visitantes. Devuelve 429 con `Retry-After` cuando está ocupada. No se confía en cabeceras
  de IP enviadas por clientes. Para varias réplicas hace falta un limitador compartido.
- Cuerpos de hasta 64 KiB; campos de documentos de hasta 4000 caracteres y 50 saltos de
  línea. Respuestas 413/422 cuando se exceden.
- Descargas con `Cache-Control: private, no-store`. Caducan a las 24 horas; el borrado se
  ejecuta al arrancar y cada hora, y se impide descargar un documento ya caducado.
- El navegador conserva el historial localmente. El aviso de demo pide datos ficticios.
- No hay cuentas ni aislamiento de documentos por usuario: un enlace funciona como acceso
  al archivo hasta su caducidad. Esto es una demo, no un servicio para datos personales reales.
- El generador produce ejemplos, no documentos emitidos por una dependencia. Las respuestas
  y citas requieren revisión humana; la evaluación del MVP detectó casos de recuperación,
  interpretación y formato de citas pendientes de corregir.

El chat depende de que el servidor de Qwen y su red privada estén disponibles. `/healthz`
no consulta Qwen para evitar reinicios inútiles cuando la máquina del modelo está apagada.

## Dictado por voz

El micrófono usa `SpeechRecognition` o `webkitSpeechRecognition` del navegador, en
español (`es-MX`). Requiere HTTPS (o localhost), permiso del micrófono y que el navegador
ofrezca un servicio de reconocimiento operativo. La compatibilidad se detecta al cargar
la página; cuando no está disponible, se conserva la escritura y se muestra una explicación.

La presencia de la API no garantiza que su servicio funcione. Brave puede exponerla
y devolver `network` por la falta de un servicio compatible; es una
[limitación documentada por Brave](https://github.com/brave/brave-browser/issues/3725).
En ese caso se indica probar Chrome o Safari, o el dictado del teclado del dispositivo.
No se reintenta en bucle ni se modifica la conexión del usuario. El error por sí solo
no permite concluir que falte Internet. No hay un motor de transcripción alternativo
en el backend.

El proveedor del navegador puede procesar el audio en sus servidores. Ventanilla no
recibe ni guarda audio: el texto permanece en el borrador hasta que la persona pulsa
**Enviar**.

- Pulsar el micrófono inicia la escucha; pulsarlo de nuevo termina y permite revisar.
- «Cancelar» restaura el borrador anterior. Los resultados tardíos se descartan.
- La escucha se detiene tras 3 segundos de silencio continuo. Un detector local con
  `getUserMedia` y Web Audio mide el nivel del micrófono cada 100 ms (umbral RMS 0.01);
  el sonido reinicia la cuenta. No depende de cuándo llegan las transcripciones y
  no graba ni envía el audio que analiza. El ruido de fondo puede retrasar la parada.
- La cuenta empieza cuando el reconocimiento y la captura están activos, sin contar
  la espera del permiso. Si el detector no está disponible o se interrumpe, se conserva
  la parada manual anterior. El estado visible indica cuándo se detecta silencio.
- Se conserva el máximo de un minuto y la parada al ocultar o abandonar la página.
  Al terminar, cancelar o fallar se liberan también el detector y su micrófono.
- Los errores de permisos, silencio o red no bloquean el teclado.
- No se reinicia automáticamente el micrófono cuando el navegador termina una sesión.

Pruebas del controlador, sin dependencias ni acceso al micrófono:

```sh
node --test tests/voice.test.mjs tests/silence.test.mjs
```

La prueba manual final debe hacerse desde HTTPS con un micrófono real: permitir el
acceso, hablar más de 3 segundos, pausar menos de 3 segundos, seguir hablando y luego
guardar silencio 3 segundos; corregir y enviar. Comprobar también la cancelación, el
permiso denegado y ruido de fondo. Las pruebas simuladas no miden la precisión ni la disponibilidad
del servicio de voz del dispositivo.

Referencia: [SpeechRecognition en MDN](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition).
Detector: [AnalyserNode en MDN](https://developer.mozilla.org/en-US/docs/Web/API/AnalyserNode/getFloatTimeDomainData).

## Docker Compose sin Coolify

Configura `.env` desde `.env.example` y una URL de Ollama alcanzable. Para publicar el chat
solo en loopback, usa un archivo de override fuera del repositorio:

```yaml
services:
  app:
    ports:
      - "127.0.0.1:8700:8700"
```

```sh
docker compose -f compose.yaml -f /ruta/override.yaml up --build -d
docker compose logs -f app
```

Los volúmenes sobreviven a un redeploy o a `docker compose down`. No uses `down -v` si
quieres conservarlos. Haz respaldos del índice fuera de los repositorios públicos.
