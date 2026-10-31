# Pendientes del flujo de dictámenes

Hallazgos de la revisión adversarial (31-oct-2026) **aún sin corregir**, de mayor a menor severidad.
Todos se reprodujeron, salvo los marcados *sin verificar*. Ninguno afecta el flujo normal con oficios legítimos;
la mayoría son de robustez ante archivos maliciosos y conviene corregirlos **antes de exponer la app fuera de Tailscale**.

## 1. Un PDF de una página con tabla grande congela todo el servidor ~9 min (CPU, GIL, sin timeout ni cancelación)
- **Severidad:** 🔴 alta (sin verificar) · **Área:** api-security · **Archivo:** `app/dictamen/extract.py`:229
- **Escenario:** Cualquiera que llegue al servidor sube un PDF de una página y 540 KB con una cuadrícula de 80x40 celdas. Ese PDF tiene texto, así que entra en modo texto. Durante ~9 minutos el servidor no atiende nada: ni GET /healthz (el HEALTHCHECK de Docker, con timeout de 3 s, falla), ni archivos estáticos, ni los streams SSE del chat. Con 4 páginas así (max_pages=4) serían ~35 min. Con dos subidas a la vez, todo POST recibe 429.
- **Corrección propuesta:** - Ejecutar prepare() en un proceso aparte (ProcessPoolExecutor o multiprocessing) con un tope duro de 30–60 s, matar el proceso al vencer y aplicarle RLIMIT_AS. Eso evita a la vez la inanición del GIL, el CPU sin límite y la memoria.
- Además, antes de find_tables, saltar la detección de tablas si la página tiene más de N palabras o trazos vectoriales (p. ej. len(page.get_text('words')) > 3000 o muchos caminos en get_drawings), y acotar las celdas procesadas (p. ej. 1500).
- Evitar un get_text(clip) por celda: extraer las palabras una sola vez por página y asignarlas a las celdas.

## 2. Bomba ZIP dentro del límite de 200 MB: un DOCX de 615 KB lleva el proceso a 4,3 GB de RSS
- **Severidad:** 🔴 alta · **Área:** api-security · **Archivo:** `app/dictamen/extract.py`:53
- **Escenario:** Un DOCX de 615 KB con ~190 MB de párrafos lleva el proceso de 1,2 a 4,3 GB en 12 s y devuelve 200, mandando además 60 000 caracteres al LLM. compose.yaml fija mem_limit: 1g al servicio app, y docs/deployment.md explica cómo activar el dictamen en Docker. Allí basta un DOCX de 220 KB (1,29 GB) para que el kernel mate el contenedor (OOM), y con él el chat. Fuera de Docker, dos subidas simultáneas suben a ~8 GB.
- **Corrección propuesta:** Corrección mínima en app/dictamen/extract.py, dentro de prepare_docx (línea ~506), ANTES de llamar a _docx_parts: añadir un tope por parte XML y bajar el tope total.

    MAX_XML_PART = 12 * 1024 * 1024  # un oficio real ocupa decenas/cientos de KB
    ...
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED:
        raise ExtractionError("El archivo Word es demasiado grande al descomprimirlo.")
    if any(i.filename.endswith(".xml") and i.file_size > MAX_XML_PART for i in infos):
        raise ExtractionError("El archivo Word es demasiado grande al descomprimirlo.")

Y bajar MAX_ZIP_UNCOMPRESSED de 200MB a ~20-30MB. El tope por parte es lo clave: la bomba mete todo en un único word/document.xml, así que capar esa parte detiene el parseo de lxml ANTES de construir el DOM, aunque el total descomprimido siguiera bajo el límite. Opcionalmente rechazar razones de compresión anómalas (>100:1 en XML de varios MB) y, como mejora mayor, leer document.xml con iterparse en streaming cortando al llegar a MAX_TEXT_CHARS en vez de cargar el DOM completo. Nota: el hallazgo es explotable solo donde DICTAMEN_ENABLED=true (la demo pública lo trae en false); en el despliegue FGEO está activo y sin login, por eso mantengo severidad high (disparo trivial, caída total del contenedor compartido).

## 3. La tarjeta da por llenos «N/A», «-», «null», «none» o «.», y /generar los rechaza como faltantes
- **Severidad:** 🟠 media · **Área:** port-template · **Archivo:** `app/main.py`:446
- **Escenario:** Un equipo sin modelo visible: la persona escribe «N/A» (o «-») en Modelo. La tarjeta no marca nada y envía. El servidor responde 422 «Faltan datos: equipo 1: Modelo.» con faltantes ["equipo_1_modelo"], y el campo queda resaltado aunque dice «N/A». Pasa lo mismo con «---», «null», «None» y «.», y en los campos generales (cargo, nombre, oficio).
- **Corrección propuesta:** Corrección mínima: no aplicar a lo que teclea la persona la heurística de vacío pensada para el LLM.

1) En app/dictamen/schema.py, junto a clean_text, agregar:
```python
def clean_user_text(value: Any) -> str | None:
    """Texto que escribió la persona: solo quita caracteres inválidos en XML, colapsa espacios y recorta."""
    if value is None or isinstance(value, bool):
        return None
    text = _WS.sub(" ", _XML_INVALID.sub("", str(value))).strip()
    return text[:MAX_FIELD_LEN].strip() or None

def normalize_user_input(raw: Any) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    data = {name: clean_user_text(src.get(name)) for name in GENERAL_NAMES}
    data["fecha_oficio_recibido"] = normalize_fecha(data["fecha_oficio_recibido"])
    eqs = src.get("equipos") if isinstance(src.get("equipos"), list) else []
    data["equipos"] = [{n: clean_user_text(e.get(n)) for n in EQUIPO_NAMES}
                       for e in eqs if isinstance(e, dict)][:MAX_EQUIPOS]
    return data
```
No descarta filas, para que los índices equipo_N coincidan con las filas de la tarjeta.

2) En app/main.py:446, cambiar `return schema.normalize_extraction(data), own` por `return schema.normalize_user_input(data), own`. normalize_extraction se sigue usando para la respuesta del LLM en /api/dictamen/oficio.

3) En tests/test_dictamen_api.py, ajustar test_frontend_faltantes_match_python_schema para comparar con `schema.faltantes(schema.normalize_user_input(c["data"]), c["own"])`. Agregar a FALTANTES_CASES un caso con modelo 'N/A', marca '-' y una fila toda con '-'. Agregar también una prueba de API: modelo 'N/A' da 200 y el DOCX contiene 'N/A'.

Alternativa, si se quiere conservar la heurística también para lo que escribe la persona: replicarla en dictBlank (`/^(null|none|n\/a)$/i` y `/^[-_.–—]+$/`), quitar el descarte de filas en _dictamen_input y avisar en la ayuda de los campos. Es peor, porque rechaza valores legítimos que el original aceptaba.

## 4. PNG por debajo de 50 MP: archivos de 6 KB a 389 KB consumen de 294 a 653 MB al decodificarse
- **Severidad:** 🟠 media · **Área:** api-security · **Archivo:** `app/dictamen/extract.py`:177
- **Escenario:** Un PNG RGBA de 16 bits y 7071x7071 (389 KB) consume ~620 MB extra. Tras la primera lectura de un PDF (que carga el modelo de layout de pymupdf), la base del servidor es ~330–360 MB, así que una sola subida roza el límite de 1 GB del contenedor y dos simultáneas lo superan. El mismo patrón se repite en las imágenes dentro de un DOCX (_docx_images, hasta 4).
- **Corrección propuesta:** En app/dictamen/extract.py, dentro de prepare_image, acotar los píxeles que de verdad se decodifican y hacerlo ANTES de getexif():

```python
MAX_DECODED_PIXELS = 16_000_000  # PNG se decodifica completo; JPEG, tras draft()
...
    fmt = img.format
    try:
        if fmt == "JPEG" and max(width, height) > MAX_IMAGE_SIDE:
            img.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))  # img.size ya queda reducido
        if img.size[0] * img.size[1] > MAX_DECODED_PIXELS:
            raise ExtractionError(f"La imagen es demasiado grande ({width}×{height} px). Sube una foto o captura de menor resolución.")
        orientation = img.getexif().get(0x0112, 1)   # en PNG esto llama a load(): debe ir después del tope
        if (fmt in ("JPEG", "PNG") and max(width, height) <= MAX_IMAGE_SIDE ...):  # ruta rápida sin cambios
            ...
```

Hay que quitar la llamada a draft() que hoy está más abajo, porque pasa a hacerse aquí. Se mantiene el tope previo de 50 MP sobre el tamaño declarado.

El tope se aplica también a las imágenes dentro de un DOCX, porque _docx_images llama a prepare_image.

Tests nuevos:
- Un PNG de 7071×7071, solo cabecera o de color plano, se rechaza con "demasiado grande".
- Un JPEG de 49999×1000 se rechaza.
- Un JPEG de 8000×6000 sigue aceptándose y sale con un lado mayor de 2000 px o menos.

Mejora opcional: llamar a ImageOps.exif_transpose solo si orientation no es 1. Hoy copia la imagen entera aunque no haya nada que girar.

## 5. (Ya existía en main) RequestLimits ocupa la plaza antes de leer el cuerpo y no tiene timeout de lectura
- **Severidad:** 🟠 media · **Área:** api-security · **Archivo:** `app/limits.py`:42
- **Escenario:** Dos conexiones que envían Content-Length: 1000 y nunca terminan el cuerpo dejan todo POST (chat, documentos, dictamen) en 429 indefinidamente.
- **Corrección propuesta:** Mínima y concreta: acotar en el tiempo cada lectura del cuerpo con asyncio.wait_for y devolver 408 al vencer. Solo toca el bucle de bufferizado (no la fase de la app/SSE, que a propósito mantiene la plaza mientras transmite). En app/limits.py:

    import asyncio  # arriba
    READ_TIMEOUT_S = 10  # tope por receive() mientras se lee el cuerpo

    ...dentro del bucle while True (reemplaza `message = await receive()`):
        try:
            message = await asyncio.wait_for(receive(), READ_TIMEOUT_S)
        except asyncio.TimeoutError:
            return await reject(408, "La lectura del cuerpo tardó demasiado. Reintenta.")

Esto libera la plaza si un cliente no entrega el cuerpo a tiempo, cerrando el slowloris de 2 sockets. Nota: `reject` debe aceptar 408 (hoy solo añade Retry-After en 429; está bien). Complemento recomendado (no imprescindible para el mínimo): además un tope TOTAL escalado al tamaño declarado (p. ej. N s por cada MB declarado) para subidas de 15 MB, y/o tomar la plaza de concurrencia solo justo antes de llamar a la app. El per-receive wait_for por sí solo ya elimina la retención indefinida.

Severidad ajustada a medium (el hallazgo decía low): es un DoS remoto NO autenticado, trivial (2 sockets ociosos, sin payload) que tumba de forma persistente TODOS los POST (chat, documentos, dictamen) hasta que el atacante suelte, y la nueva ruta de subida lo agrava también sin ataque. Acotado a disponibilidad en una instancia demo de un solo proceso (sin pérdida de datos ni confidencialidad; los propios límites se documentan como guardas de demo y ya existía en main), por lo que no llega a high; low es defendible por ese contexto demo, pero la trivialidad y el alcance total inclinan a medium.

## 6. Las fotos de hasta 2000 px se reenvían al LLM sin recodificar, con los datos añadidos al final
- **Severidad:** 🟠 media · **Área:** api-security · **Archivo:** `app/dictamen/extract.py`:172
- **Escenario:** Un JPEG de 1200x1600 (~100 KB reales) con 14 MB de basura al final hace que el servidor mande una petición de 19,6 MB a Ollama. Eso gasta ancho de banda y memoria del LLM compartido, y reenvía contenido arbitrario oculto en el archivo.
- **Corrección propuesta:** Corrección mínima en app/dictamen/extract.py; la probé en una copia y la suite sigue pasando (304 passed, 1 skipped):

1) Usar el atajo solo para archivos pequeños; si no, recodificar siempre. Esto descarta datos después de EOI/IEND, chunks auxiliares y segmentos COM/APP. Hay que descartar la condición "o termina en EOI/IEND": no basta.

```python
MAX_PASSTHROUGH_BYTES = 3 * 1024 * 1024  # más grande que esto se recodifica (quita datos extra)
MAX_DOCX_IMAGE_BYTES = 20 * 1024 * 1024  # imágenes del Word más pesadas no se leen
...
        if (
            fmt in ("JPEG", "PNG") and max(width, height) <= MAX_IMAGE_SIDE
            and len(content) <= MAX_PASSTHROUGH_BYTES
            and orientation in (None, 1) and img.mode in ("RGB", "L")
        ):
            return (JPEG_MIME if fmt == "JPEG" else PNG_MIME), content
```

2) En _docx_images, no leer entradas de medios enormes. Sin esto, un solo word/media de unos 199 MB obliga a hacer zf.read de 200 MB aunque luego se recodifique:

```python
    for name in sorted(...):
        if zf.getinfo(name).file_size > MAX_DOCX_IMAGE_BYTES:
            continue
        data = zf.read(name)
```

Una alternativa aún más simple sería eliminar el atajo y recodificar siempre (JPEG con quality 90, PNG sin cambios). Cuesta unos 100 ms por imagen.

Recomiendo un test de regresión con un JPEG que lleve 5 MB al final (que la salida mida menos de 1 MB) y otro con un DOCX de 4 imágenes con relleno (que la petición al LLM mida menos de unos 16 MB).

## 7. Con «.» como separador, los nombres con inicial intermedia o título dan iniciales erróneas o duplicadas
- **Severidad:** 🟡 baja · **Área:** port-template · **Archivo:** `app/dictamen/tecnicos.py`:132
- **Escenario:** Con el catálogo actual (nombres vacíos), «Iván G. Flores» da «ig/flores» y el oficio imprime «MDTG/ig/flores» (el original daba «igf»). «Luis A. Fernández» da «la/fernandez» (original «laf»). «Lic. Diana Sánchez» da «lic/ds» (original «lds»). Con nombres llenos en tecnicos.json, «Iván G. Flores» da «igf/igf» y «Luis A. Fernández» da «lafg/lafg»: el mismo técnico dos veces en «MDTG/igf/igf».
- **Corrección propuesta:** En app/dictamen/tecnicos.py: tratar «.» como separador solo si ningún punto va seguido de espacio y más texto («igf.dsa»), o si todas las piezas son iniciales conocidas del catálogo («igf. dsa.»). Si no, se lee como UN nombre, con los puntos cambiados por espacios. Esto es lo que probé en la copia:

```python
_DOT_NOMBRE = re.compile(r"\." + _WS + r"+\S")  # punto seguido de espacio y más texto

def _tokens(texto: str, separadores_extra: bool, conocidas: frozenset[str] = frozenset()) -> list[str]:
    ...
        if "." in pieza and _DOT_ABBR.fullmatch(pieza):
            partes = [_WORDS.sub("", pieza.replace(".", ""))]  # i.g.f. -> igf
        elif _DOT_NOMBRE.search(pieza) and not all(
            _norm(p) in conocidas for p in pieza.split(".") if _trim(p)
        ):
            partes = [_WORDS.sub(" ", pieza.replace(".", " "))]  # "Iván G. Flores", "Lic. Diana": un nombre
        else:
            partes = pieza.split(".")
```

En _resolver: `tokens = _tokens(texto, separadores_extra, frozenset(_norm(x["iniciales"]) for x in tecnicos))`.

Actualizar el docstring de la corrección 2.

Tests:
- test_named_catalog_ambiguous_keeps_js_message: cambiar «D. Sánchez» por «D» o «D.» para conservar el caso ambiguo.
- Agregar con ACTUAL: «Iván G. Flores»→igf, «Luis A. Fernández»→laf, «Lic. Diana Sánchez»→lds, «D. Sánchez»→ds.
- Agregar con NOMBRES: «Iván G. Flores»→igf, «Luis A. Fernández»→lafg, «D. Sánchez»→dsa.

Deduplicar `out` es opcional y no hace falta con esta corrección. El JS conserva «igf/igf» cuando la persona lo escribe así.

## 8. Cada dictamen hereda las fechas y estadísticas de la plantilla (modificado el 07/09/2026, revisión 24)
- **Severidad:** 🟡 baja · **Área:** port-template · **Archivo:** `scripts/convert_dictamen_template.py`:429
- **Escenario:** Un dictamen generado el 31/10/2026 muestra en Archivo > Información de Word «Modificado: 07/09/2026», «Revisión 24» y 537 minutos de edición. Todos los dictámenes comparten esas fechas y el mismo docId, lo que puede confundir si alguien revisa la procedencia o el orden de los documentos.
- **Corrección propuesta:** Corrección mínima en app/dictamen/generate.py, en render_dictamen, dentro del try, justo después de template.render(...) y antes de template.save(buffer):

    from datetime import datetime, timezone  # arriba del módulo
    ...
    cp = template.docx.core_properties
    # python-docx escribe strftime('...Z') e ignora tzinfo: hay que pasar la hora en UTC.
    ahora = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    cp.created = ahora
    cp.modified = ahora
    cp.revision = 1

No uses la hora de America/Mexico_City para estos campos: quedaría 6 h desfasada y etiquetada como Z. La zona de México solo aplica a fecha_larga, que ya está bien. Este cambio no requiere volver a convertir la plantilla, y ningún test compara bytes entre dos renders. Conviene un test: core_properties.revision == 1 y que modified sea la fecha de hoy en UTC.

Opcional, en scripts/convert_dictamen_template.py, en clean_app: poner TotalTime en "0" (solo si es distinto de 0, para mantener la idempotencia) y quitar Pages, Words, Characters, Lines, Paragraphs y CharactersWithSpaces, que son opcionales en el esquema. Después hay que regenerar config/dictamen/dictamen.docx, porque test_plantilla_convertida_al_dia compara contra la salida del conversor, y actualizar el docstring. Quitar o regenerar w14:docId y w15:docId no aporta mucho y se puede omitir.

## 9. Las subidas ocupan las 2 plazas globales y las retienen aunque el cliente cancele: chat y /api/documents en 429
- **Severidad:** 🟡 baja · **Área:** api-security · **Archivo:** `app/main.py`:369
- **Escenario:** Dos subidas canceladas por el cliente a 1 s dejan el chat y /api/documents en 429 durante toda la llamada al LLM. Con Qwen colgado son hasta ~245 s la primera y ~490 s la segunda, y basta con repetir para mantener el servicio caído sin enviar ningún archivo pesado.
- **Corrección propuesta:** Cambio mínimo, solo en el endpoint dictamen_oficio de app/main.py; ya lo validé en una copia y la suite sigue pasando:

```python
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
# en lifespan: app.state.dictamen_busy = asyncio.Lock()  (mejor que un Lock a nivel de módulo)
...
    busy = request.app.state.dictamen_busy
    if busy.locked():  # Qwen atiende en serie: no ocupes una plaza global esperando en cola
        return JSONResponse(status_code=429, headers={"Retry-After": "30"},
                            content={"message": "Ya se está leyendo otro oficio. Intenta de nuevo en un momento."})
    try:
        async with busy:
            task = asyncio.create_task(st.dictamen_extractor.extract(content, filename))
            while not (await asyncio.wait({task}, timeout=0.5))[0]:
                if await request.is_disconnected():  # «Detener», recarga o «Nueva conversación»
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                    return Response(status_code=499)
            result = task.result() or {}
        data = schema.normalize_extraction(result.get("data"))
    except extract.ExtractionError as exc: ...  (sin cambios)
```

Qué consigue:
- Una subida usa como máximo 1 de las 2 plazas, así que el chat siempre conserva al menos una.
- La segunda subida recibe un «ocupado» inmediato en vez de quedarse en cola ocupando plaza, de modo que su timeout ya no se suma al de la primera.
- Si el cliente se desconecta, la tarea se cancela: se libera la plaza y httpx cierra la conexión con Ollama, que deja de generar.

Opcional:
- Esperar hasta ~2 s en el acquire (`asyncio.wait_for(busy.acquire(), 2)`), o que el frontend reintente una vez ante un 429, para cubrir el caso de volver a subir en menos de 0,5 s.
- Como mejora mayor, que RequestLimits reciba en route_limits una concurrencia propia por ruta y rechace con 429 antes de bufferizar 15 MB.

En el informe conviene corregir dos cosas: el bloqueo total del chat dura una sola extracción (no ~490 s), y el vector de DoS por atacante ya existía por el presupuesto global de 30 POST/min.

## 10. Con dictamen_enabled=false la demo pública sigue aceptando y almacenando cuerpos de 15 MB, y revela la función
- **Severidad:** 🟡 baja · **Área:** api-security · **Archivo:** `app/main.py`:156
- **Escenario:** Antes de este cambio, la demo pública cortaba todo POST a 64 KB. Ahora acepta 15 MB por petición en una ruta, hasta 30 por minuto (~450 MB/min de entrada, memoria y temporales en disco), y un 413 o /api/health delatan que existe una función interna de la Fiscalía.
- **Corrección propuesta:** Corrección mínima en app/main.py (líneas 156-161): registrar la excepción de tamaño solo cuando la función está activa en la configuración.

    route_limits={
        "/api/dictamen/oficio": (
            _settings.dictamen_max_upload_bytes + UPLOAD_OVERHEAD,
            f"El archivo supera el máximo de {_settings.dictamen_max_upload_mb:g} MB.",
        ),
    } if _settings.dictamen_enabled else {},

Así, con DICTAMEN_ENABLED=false todo POST vuelve a cortarse en max_request_bytes (64 KiB) desde el middleware, antes de bufferizar o parsear el multipart. Los cuerpos pequeños siguen recibiendo 404. Los tests actuales pasan, incluido test_app_registers_upload_limit_from_settings, porque el valor por defecto es True.

Opcionales:
- Para cubrir el caso en que DICTAMEN_ENABLED=true pero _load_dictamen lo apaga (falta app/dictamen en la imagen), el middleware puede consultar scope["app"].state.dictamen_enabled antes de aplicar route_limits.
- En /api/health, devolver "template" solo si enabled es true. Es cosmético: openapi.json ya lista las rutas.

## 11. JSON con NaN/Infinity en un campo inválido produce 500 no controlado y reinicia la conexión
- **Severidad:** 🟡 baja · **Área:** api-security · **Archivo:** `app/main.py`:471
- **Escenario:** `{"data": NaN, "own": {}}` o `{"conversation_id": Infinity}` en /api/dictamen/generar dan 500 con traza completa en el log. Con keep-alive, la siguiente petición en esa conexión recibe un reset (lo vi en el cliente httpx).
- **Corrección propuesta:** Corrección mínima: un manejador de RequestValidationError igual al de FastAPI pero que no devuelve "input". Va en app/main.py, después de `app.mount("/static", ...)`, con dos imports nuevos:

```python
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """Como el de FastAPI, pero sin "input": puede traer NaN/Infinity (json.loads los acepta)
    y JSONResponse no los serializa -> 500 y conexión cortada."""
    errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
```

Lo probé en una copia del worktree (/tmp/dictamen-int/verify-api-security-5/wt) y la reproducción quedó así: los 7 casos dan 422 limpios, la conexión keep-alive sigue viva (la siguiente petición da 200) y el log tiene 0 «Exception in ASGI application». La suite completa en la copia parchada da 304 passed, 1 skipped. Ni el frontend ni los tests leen "input" (app.js solo lee `detail` si es una cadena), así que quitarlo no rompe nada. Además deja de devolver lo que mandó el cliente.

Observación aparte, menor y no forma parte del hallazgo: `_dictamen_input.text()` acepta números de tipo float, y un NaN dentro de un campo de texto (`{"data":{"oficio_recibido":NaN}}`) se convierte en la cadena "nan" en lugar de rechazarse. Si se quiere cerrar eso, `text()` podría rechazar los float no finitos con `math.isfinite`.

## 12. sniff_mime abre el ZIP de forma síncrona en el event loop (dos veces): 0,4–1,2 s de bloqueo por petición
- **Severidad:** 🟡 baja · **Área:** api-security · **Archivo:** `app/main.py`:363
- **Escenario:** Un ZIP de 13,5 MB con 150 000 entradas vacías y word/document.xml congela todas las respuestas entre 0,42 y 1,17 s por petición, aunque al final se rechace con 502. Con 30 peticiones por minuto, eso deja ~30 s de cada minuto con el servidor parado.
- **Corrección propuesta:** Move both sniff_mime calls off the event loop and sniff once. Minimal concrete change: (1) in the /api/dictamen/oficio endpoint (app/main.py:363) replace `extract.sniff_mime(content, filename)` with `await asyncio.to_thread(extract.sniff_mime, content, filename)` (asyncio is already imported); (2) in OficioExtractor.extract (extract.py ~662) stop calling sniff_mime on the loop before to_thread -- either fold the mime detection into the threaded prepare() (have prepare sniff and raise ExtractionError on None), or add an optional `mime=` parameter so the already-computed value from the endpoint is passed in. This keeps every ZIP central-directory scan inside a worker thread and runs it only once per request, eliminating the event-loop stall. Optionally, as defense-in-depth, read the EOCD record to reject ZIPs declaring more than MAX_ZIP_ENTRIES before constructing ZipFile. Severity stays low: per-request stall is sub-second, bounded by RequestLimits (30/min, 2 concurrent), and DICTAMEN_ENABLED defaults to false on the public demo.

## 13. Cada dictamen ocupa 740–785 KB en disco durante 24 h sin autenticación: hasta ~33 GB/día
- **Severidad:** 🟡 baja · **Área:** api-security · **Archivo:** `app/main.py`:514
- **Escenario:** Un script que llame a /api/dictamen/generar 30 veces por minuto acumula 43 200 × ~0,76 MB ≈ 33 GB en 24 h dentro del volumen app-data, hasta llenar el disco del host interno.
- **Corrección propuesta:** Corrección mínima: poner una cuota global de almacenamiento en DocumentConnector, así protege también /api/documents.

1) app/settings.py: añadir `max_generated_storage_mb: float = 2048`.

2) app/documents/connector.py: añadir el parámetro `max_storage_bytes: int | None = None` en `__init__` y, en `store_file`, después de `self._check_size(content)`:
```python
if self.max_storage_bytes is not None:
    used = sum(p.stat().st_size for p in self.storage_dir.glob("*.bin"))
    if used + len(content) > self.max_storage_bytes:
        self.purge_expired()
        used = sum(p.stat().st_size for p in self.storage_dir.glob("*.bin"))
        if used + len(content) > self.max_storage_bytes:
            raise DocumentServiceError("El almacenamiento temporal de documentos está lleno; inténtalo más tarde.")
```
Con 2 GB caben unos 2800 dictámenes, y recorrer el directorio en cada llamada cuesta pocos milisegundos.

3) app/main.py, en lifespan: pasar `max_storage_bytes=int(s.max_generated_storage_mb * 1024 * 1024)`. En dictamen_generar, antes del `except Exception`, añadir `except DocumentServiceError as exc: return JSONResponse(status_code=503, content={"message": str(exc)})`. /api/documents ya convierte DocumentServiceError en 502.

Complemento recomendado: dar a los dictámenes una retención corta, de unas 1–2 h, porque se descargan en el momento. Se puede guardar `expires_at` en el JSON de metadatos según el tipo y respetarlo en get() y purge_expired(). O sumar un límite propio de generaciones por minuto para la ruta. Ninguna de estas dos medidas sola acota el disco; la cuota sí.

Documentar la variable MAX_GENERATED_STORAGE_MB en docs/deployment.md.

## 14. «Corregir» y luego «Descartar» deja una nota falsa para el LLM («sin generar el dictamen») aunque el DOCX ya se generó
- **Severidad:** 🟡 baja · **Área:** frontend · **Archivo:** `app/static/app.js`:269
- **Escenario:** 1. Generar el dictamen (sale 200, aparece la tarjeta de descarga y la nota dice que se generó con sus números e iniciales). 2. Pulsar «Corregir datos y volver a generar». 3. Pulsar «Descartar». La tarjeta muestra «Tarjeta descartada.» y la descarga sigue visible, pero history[1] queda como «… La persona descartó la tarjeta sin generar el dictamen.». Ese texto se envía al LLM en el siguiente /api/chat. Si la persona pregunta qué número quedó, el asistente responde que no se generó ningún dictamen.
- **Corrección propuesta:** Cambio mínimo en app/static/app.js, verificado en una copia.

(1) En dictHistoryNote, sustituir las líneas 265-269 por:
```js
  if (c.result) {
    const [a, b] = c.result.numeros || [];
    const gen = `${base} La persona revisó los datos y se generó el dictamen «${c.result.filename}» (${c.result.equipos} equipo(s), números ${a} a ${b}, iniciales MDTG/${c.result.iniciales}); ya puede descargarlo desde el chat.`;
    return c.status === "cancelled" ? `${gen} Después abrió la corrección y la descartó sin volver a generar: ese dictamen sigue vigente.` : gen;
  }
  if (c.status === "cancelled") return `${base} La persona descartó la tarjeta sin generar el dictamen.`;
```

(2) En sync(), cambiar la línea 636 a:
```js
if (c.status === "done" || (c.status === "cancelled" && c.result)) text = dictResultText(c.result) + (c.status === "cancelled" ? " (Corrección descartada.)" : "");
```
Así la propia tarjeta deja de decir solo «Tarjeta descartada.» cuando sí hay un dictamen.

(3) En tests/test_dictamen_api.py, agregar `assert "«d.docx»" in out["noteManual"]` y un caso sin result que siga diciendo «sin generar el dictamen».

(4) Subir ?v= de app.js en index.html.

Alternativa de mejor UX, aunque más amplia: con c.result presente y status 'pending', rotular el botón cancel como «Cancelar corrección». Al entrar en «Corregir» se guarda una copia (structuredClone de c.data/c.own); al cancelar se restaura y se vuelve a 'done' sin tocar la nota. Esto evita que los campos editados queden desalineados con el DOCX descargado.

## 15. Una fila con guiones o «N/A» en todos los campos pasa la validación del cliente, pero el servidor la descarta: el dictamen sale con menos equipos que la tabla
- **Severidad:** 🟡 baja · **Área:** frontend · **Archivo:** `app/static/app.js`:153
- **Escenario:** La tarjeta tiene la fila 1 completa. La persona agrega la fila 2 y escribe «-» en tipo, marca, modelo y serie, como «sin dato». El cliente no marca faltantes y muestra «Se numerarán del 191 al 192». El servidor responde 200 con numeros [191, 191]. El DOCX trae un solo dictamen y la segunda fila se omite sin aviso. La nota del historial dice «(2 equipo(s), números 191 a 191 …)».
- **Corrección propuesta:** Corrección mínima en el cliente, en app/static/app.js, dentro del bloque DICTAMEN-PURE (línea 142). Hay que hacer que dictBlank trate como vacío lo mismo que schema.clean_text:

const DICT_EMPTY = /^(?:null|none|n\/a|[-_.–—]+)$/i; // igual que _EMPTY_WORDS y _ONLY_DASHES en schema.py
const dictBlank = (v) => v == null || !String(v).trim() || DICT_EMPTY.test(String(v).trim());

dictClean y dictFaltantes usan dictBlank, así que con este cambio:
- las filas de guiones o N/A se marcan como faltantes y bloquean «Generar»;
- dictPayload envía null en esos campos;
- se corrige también el caso del motivo «-».

Lo probé sobre una copia (app_fixed.js). Las filas de guiones, N/A y puntos devuelven equipo_2_tipo_equipo, equipo_2_marca, equipo_2_modelo y equipo_2_numero_serie, y la fila parcial devuelve equipo_2_marca.

Como defensa en profundidad, que es opcional:
- en dictamen_generar, añadir "equipos": len(equipos) a la respuesta 200;
- en app.js:748, usar equipos: body.equipos ?? c.data.equipos.length, para que la nota del historial refleje lo que el servidor generó.

Otra opción es que /generar no descarte filas: llamar a normalize_extraction solo en /oficio, o limpiar campo por campo sin _equipo. Así los índices de faltantes siempre corresponden a las filas de la tarjeta.

## 16. Lo tecleado en los últimos 250 ms se pierde al recargar o cerrar la pestaña
- **Severidad:** 🟡 baja · **Área:** frontend · **Archivo:** `app/static/app.js`:515
- **Escenario:** Con la tarjeta pendiente, se cambia la marca del equipo 1 de «N/A» a «LENOVO» y se recarga de inmediato. Al volver, la tarjeta sigue mostrando «N/A».
- **Corrección propuesta:** Cambio mínimo en app/static/app.js, verificado en el navegador. Subir index.html a app.js?v=6.

1) Línea 18:
window.addEventListener("beforeunload", () => { unloading = true; dictFlushSave(); });
(La llamada funciona aunque dictFlushSave se declare más abajo: el manejador se ejecuta después de que el módulo termina de cargar.)

2) Sustituir las líneas 515-518 por:
const dictSaveSoon = () => {
  clearTimeout(dictSaveTimer);
  dictSaveTimer = setTimeout(() => { dictSaveTimer = null; save(); }, 250);
};
// Al recargar, cerrar o pasar a segundo plano (móvil) no se espera al temporizador.
const dictFlushSave = () => {
  if (dictSaveTimer === null) return;
  clearTimeout(dictSaveTimer);
  dictSaveTimer = null;
  save();
};
window.addEventListener("pagehide", dictFlushSave);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") dictFlushSave(); });

3) Opcional, para dejarlo limpio: en generateDictamen (línea 714), después de clearTimeout(dictSaveTimer), añadir dictSaveTimer = null;. Sin esto, el único efecto es un save() redundante e inofensivo.

El script de prueba es /tmp/dictamen-int/verify-frontend-2/repro.cjs; con FIXED_JS=/tmp/dictamen-int/verify-frontend-2/app.fixed.js valida la corrección.

## 17. La nota sintética «(Adjunto) oficio «x»» altera la búsqueda del siguiente mensaje corto del chat
- **Severidad:** 🟡 baja · **Área:** frontend · **Archivo:** `app/static/app.js`:820
- **Escenario:** Después de subir un oficio, la persona pregunta «¿Cómo saco mi e.firma?». Solo con BM25, las 6 fichas de e.firma (15/CFF, 17/CFF, 19/CFF) se reemplazan por fichas de «Aclaración…» (114/CFF, 43/CFF, 5/ISR…). Con la reescritura y los embeddings reales, el efecto se atenúa pero no desaparece: la ficha 166/CFF (consulta de verificación) sube al primer lugar y se pierde la ficha 16/CFF de e.firma. Si RAG_QUERY_REWRITE está apagado o la reescritura falla, la degradación es total.
- **Corrección propuesta:** Corrección mínima en el backend, en app/chat.py, función retrieval_query. Si el mensaje user anterior es la nota sintética del adjunto, no se concatena:

    if len(last.split()) < 8 and len(users) > 1 and not users[-2].startswith("(Adjunto)"):
        return f"{users[-2]}\n{last}"

Verifiqué esta versión con fix_check.py:
- Tras el dictamen devuelve '¿Cómo saco mi e.firma?', la misma consulta que sin dictamen.
- Conserva el comportamiento existente con preguntas previas reales: con '¿Cuánto cuesta el pasaporte?' y luego '¿y para menores?', sigue concatenando ambas.

Ningún test hace referencia a retrieval_query ni a "(Adjunto)", así que el cambio no rompe la suite. Conviene añadir un test unitario con la historia [user "(Adjunto) oficio «x»", assistant "(Sistema) …", user "¿Cómo saco mi e.firma?"] que espere solo la última pregunta.

Una corrección solo en el frontend, cambiando la redacción de la nota, no basta: cualquier palabra (incluido el prefijo «adjunt*») sigue entrando en BM25 y en el embedding. El rewriter no necesita cambios: con la nota en el contexto produjo una consulta correcta.

## 18. Al quitar o agregar una fila se borran las marcas del servidor, pero su mensaje de error sigue visible
- **Severidad:** 🟡 baja · **Área:** frontend · **Archivo:** `app/static/app.js`:222
- **Escenario:** 1. Generar con iniciales «MDTG»: aparece el 422 «Escribe las iniciales del técnico; "MDTG/" ya está en la plantilla.» con el campo marcado. 2. Quitar cualquier fila. El campo de iniciales deja de estar en rojo, pero el mensaje sigue igual. Lo mismo pasa con «Faltan datos: al menos un equipo» después de pulsar «+ Agregar equipo»: el error no se actualiza hasta la siguiente edición.
- **Corrección propuesta:** La corrección mínima va en app/static/app.js y la probé con funciones puras: compárense /tmp/dictamen-int/verify-frontend-4/pure.js y pure_fixed.js usando t.js.

```js
function dictAddEquipo(c) {
  if (c.data.equipos.length >= c.maxEquipos) return false;
  c.data.equipos.push(dictEmptyEquipo(c.fields));
  if (!(c.invalid || []).length) c.error = "";
  return true;
}

function dictRemoveEquipo(c, i) {
  if (!(i >= 0 && i < c.data.equipos.length)) return false;
  c.data.equipos.splice(i, 1);
  // los índices de las filas cambian: solo se descartan las marcas de filas
  c.invalid = (c.invalid || []).filter((k) => !/^equipo(s$|_\d+_)/.test(k));
  if (!c.invalid.length) c.error = "";
  return true;
}
```

Resultado:
- Después del 422 de iniciales, quitar o agregar una fila conserva la marca roja de «iniciales» y su mensaje, así que ambos siguen siendo coherentes.
- El error del cliente "Completa los N…" se borra al agregar o quitar filas, y sync() vuelve a mostrar "Faltan N dato(s)" con el número recalculado.

Variante opcional: si el 422 era de faltantes con claves equipo_* y se quitó una fila, también podría ponerse c.error = "", porque su texto menciona filas renumeradas. Las claves que no son de filas siguen marcadas de todos modos.

## 19. Con 100 equipos y motivos largos, «Generar» responde 413 con un mensaje que lleva a perder la tarjeta
- **Severidad:** 🟡 baja · **Área:** frontend · **Archivo:** `app/static/app.js`:740
- **Escenario:** Un oficio con 100 equipos y motivos de unos 500 caracteres (el cuerpo pesa 69 808 bytes) devuelve 413 y la tarjeta dice «…inicia otra conversación». Si la persona sigue la indicación, pierde lo capturado. Con motivos de 250 caracteres (44 608 bytes) sí genera.
- **Corrección propuesta:** La corrección mínima va en el servidor, en app/main.py, dentro de route_limits de app.add_middleware(RequestLimits, ...). Hay que añadir una segunda entrada:

    "/api/dictamen/generar": (
        1024 * 1024,  # cubre 100 equipos × 6 campos + 8 campos, × 500 caracteres × 3 bytes UTF-8 ≈ 0,9 MB
        "Hay demasiado texto para un solo dictamen. Acorta los motivos por equipo (o usa el motivo general) o divide los equipos en dos dictámenes.",
    ),

Con eso el tope de la ruta concuerda con su validación (DICTAMEN_MAX_TEXT=500, MAX_EQUIPOS=100). El mensaje ya no sugiere iniciar otra conversación, y el chat y los demás POST siguen en max_request_bytes.

Complementos recomendados:
1. Un test en tests/test_dictamen_api.py: 100 equipos con motivo de 500 caracteres en /api/dictamen/generar no debe dar 413 (con el render simulado, 200), y /api/chat con más de 64 KiB debe seguir dando 413 con TOO_LARGE.
2. Mencionar esta segunda excepción en docs/deployment.md, en «Límites de la demo» (líneas 50-52).
3. Opcional, en app.js:740: si res.status === 413, mostrar un texto propio en lugar de dictErrorText, por si un proxy delante devuelve 413 sin JSON. Por ejemplo, c.error = "Hay demasiado texto para un solo dictamen; acorta los motivos por equipo o usa el motivo general.".

## Otros pendientes conocidos

- `.dockerignore` (de `main`) excluye `app/dictamen/` y no incluye `config/dictamen/`: la imagen Docker no puede activar el dictamen.
- Instancias sin compose (`scripts/dev.sh`, `install-systemd.sh`) arrancan con `DICTAMEN_ENABLED=true` y sin autenticación.
- Erratas del texto fijo de la plantilla (no corregidas por decisión): «le envió» → «le envío»; «oficio … remitida» → «remitido»; «NUMERO DE INVENTARIO» → «NÚMERO»; «san Bartolo» → «San Bartolo» (pie).
- Revisar un dictamen generado en Word (aquí no hay Word ni LibreOffice; la equivalencia se verificó por XML).
- Probar con oficios reales (sellos, firmas, fotos de celular); las pruebas usaron oficios sintéticos.
- Declarar Pillow en requirements.txt (hoy llega como dependencia de fpdf2).
