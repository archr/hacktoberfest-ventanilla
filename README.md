# Ventanilla — chat de trámites y leyes de México (MVP)

Chat ciudadano que responde dudas sobre **trámites de gobierno** (p. ej. "¿cómo saco mi
constancia de situación fiscal?") y sobre **qué leyes aplican** a una situación ("me
despidieron sin causa, ¿qué me corresponde?"). Responde con base en **fuentes oficiales**,
cita cada dato con un enlace a la página exacta del PDF, y puede **generar documentos**
(carta poder, escrito libre ante el SAT, solicitud de información pública) llamando a un
servicio externo, solo cuando la persona lo pide y confirma los datos.

- **LLM open-weight local**: Qwen (`qwen3.8:27b-mlx`) servido por Ollama en la Mac Studio,
  vía la API compatible con OpenAI. Funciona igual con vLLM, LM Studio o llama.cpp.
- **RAG híbrido**: BM25 (SQLite FTS5, sin acentos y con pseudo-stemming) + embeddings
  (`qwen3-embedding:0.6b`), fusionados con RRF, más reescritura de la pregunta coloquial a
  terminología jurídica con el propio Qwen.
- **Documentos**: cada tipo de documento definido en YAML se vuelve una *tool* del LLM. El
  LLM reúne los datos, el usuario los revisa y corrige en una tarjeta, y al presionar
  **Generar documento** se llama al servicio externo.

```
Navegador ──SSE──► FastAPI (app/main.py)
                      │
                      ├─ ChatService (app/chat.py)
                      │    1. reescribe la pregunta (Qwen, sin razonamiento)
                      │    2. busca: BM25 + embeddings → RRF (app/rag/)
                      │    3. Qwen responde con citas [n]; si piden un documento → tool call
                      │    4. tool call válida → tarjeta de confirmación en la UI
                      │
                      ├─ POST /api/documents ─► DocumentConnector ─► servicio de documentos
                      │                          (app/documents/)     (mock: mock_doc_service/)
                      └─ Ollama (Mac Studio) ◄── chat + embeddings
```

## Fuentes indexadas

| Fuente | Parser | Unidad de cita |
|---|---|---|
| Anexo 2 de la RMF 2026 — fichas de trámites fiscales del SAT (DOF 28-12-2025) | `sat_fichas` | ficha (p. ej. *1/CFF*, *14/CFF*) |
| Constitución, CFF, LISR, LIVA, LFDC, LFPA, LFT, LSS, LFPC (texto vigente, Cámara de Diputados) | `ley` | artículo, con Título/Capítulo |

Son ~5,900 fragmentos. Cada cita enlaza al PDF oficial en la página exacta (`...pdf#page=47`).
Para agregar fuentes, edita [`config/sources.yaml`](config/sources.yaml) y corre la ingesta
de esa fuente (ver abajo).

## Puesta en marcha

Requisitos: Python ≥ 3.11 y un Ollama accesible con los modelos de chat y embeddings.

```bash
cd gobchat
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # ajusta LLM_BASE_URL, modelos, etc.

# En la máquina de Ollama (una sola vez)
ollama pull qwen3-embedding:0.6b   # el modelo de chat ya debe existir

# 1) Construir el índice (descarga PDFs oficiales → parsea → embeddings). ~4 min.
.venv/bin/python -m app.rag.ingest
#    solo algunas fuentes:          --only cff lft
#    volver a descargar:            --refresh
#    sin embeddings (solo BM25):    --no-embed

# 2) Levantar el chat (:8700) y el servicio de documentos simulado (:8701)
./scripts/dev.sh
# → http://127.0.0.1:8700

# Pruebas
.venv/bin/python -m pytest -q
```

## Configuración (`.env`)

Para una demo en Docker/Coolify, consulta [Despliegue](docs/deployment.md).

| Variable | Por defecto | Descripción |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:11434/v1` | API compatible con OpenAI (Ollama: `.../v1`) |
| `LLM_MODEL` | `qwen3.8:27b-mlx` | Modelo de chat |
| `LLM_REASONING_EFFORT` | `none` | `none` = sin razonamiento (respuesta inmediata). Vacío = razonamiento del modelo (~20-30 s extra, se muestra "Pensando…") |
| `EMBED_BASE_URL` / `EMBED_MODEL` | igual que el LLM / `qwen3-embedding:0.6b` | Embeddings. Si cambias el modelo, **reindexa** |
| `RAG_TOP_K` | `6` | Fragmentos que recibe el LLM por pregunta |
| `RAG_QUERY_REWRITE` | `true` | Reescritura de la pregunta con terminología jurídica |
| `DOC_SERVICE_URL` / `DOC_SERVICE_TOKEN` | `http://127.0.0.1:8701` / vacío | Servicio de documentos y su token (Bearer) |
| `CONFIRM_DOCUMENTS` | `true` | Tarjeta de confirmación antes de llamar al servicio |

## Conectar tu servicio de documentos

Los tipos de documento y los servicios se configuran en
[`config/document_services.yaml`](config/document_services.yaml). Cada documento declara su
`id`, título, descripción (la lee el LLM), el servicio, la ruta y un **JSON Schema** con los
parámetros. Ese esquema se usa para la tool del LLM, la validación y el formulario de
confirmación. Para conectar el servicio real, cambia `DOC_SERVICE_URL` (y el token) y ajusta
`request.path` si hace falta.

### Contrato

```http
POST {base_url}{path}            (por defecto /v1/documents)
Authorization: Bearer <DOC_SERVICE_TOKEN>      (opcional; también "header" con nombre propio)
Content-Type: application/json

{
  "type": "carta_poder",
  "data": { "otorgante_nombre": "…", "apoderado_nombre": "…", … },
  "metadata": { "conversation_id": "…", "requested_at": "2026-10-31T19:40:00+00:00", "source": "gobchat" }
}
```

El conector acepta cualquiera de estas respuestas:

| Respuesta | Forma |
|---|---|
| Lista (JSON) | `{"status":"ready","download_url":"/v1/documents/abc/file","filename":"carta.pdf","mime_type":"application/pdf"}` (`download_url` puede ser relativa o absoluta) |
| Inline (JSON) | `{"status":"ready","content_base64":"JVBERi0…","filename":"…","mime_type":"…"}` |
| Asíncrona | `{"status":"processing","status_url":"/v1/jobs/123"}` → el conector consulta `status_url` hasta `ready` (dentro de `timeout_s`) |
| Archivo directo | cuerpo binario con `Content-Type: application/pdf` (u `octet-stream`, docx…) y `Content-Disposition` opcional |
| Error | HTTP ≥ 400, o `{"status":"error","error":"mensaje"}` |

El archivo siempre se descarga y se guarda en `data/generated/`, y el navegador lo baja de
`/api/documents/{id}`. Así funciona aunque el servicio use URLs internas, firmadas o con
expiración. El token **solo** se envía al mismo origen del servicio. Una `download_url` en
otro dominio (p. ej. S3 firmada) se descarga sin credenciales, y lo mismo pasa con las
redirecciones.

Para probar tu servicio sin el chat:

```bash
curl -s localhost:8700/api/documents -H 'Content-Type: application/json' \
  -d '{"type":"carta_poder","data":{"otorgante_nombre":"A","apoderado_nombre":"B","dependencia":"SAT","tramite":"recoger e.firma","testigo1_nombre":"C","testigo2_nombre":"D","lugar":"CDMX"}}'
```

## Estructura

```
app/
  main.py            API (SSE /api/chat, /api/documents, /api/health…) y frontend estático
  chat.py            orquestación de un turno (RAG → LLM → tools → confirmación)
  llm.py             clientes OpenAI-compatibles (chat en streaming, completions, embeddings)
  prompts.py         prompt del sistema y formato de fuentes
  rag/               ingest, limpieza de PDFs, parsers (ley / fichas SAT / html), índice, búsqueda, reescritura
  documents/         registro de documentos (YAML → tools + validación) y conector HTTP
  static/            UI (HTML/CSS/JS sin dependencias)
mock_doc_service/    servicio simulado que implementa el contrato y genera PDFs (fpdf2)
config/              sources.yaml, document_services.yaml
scripts/dev.sh       levanta mock + chat
tests/               pruebas (pytest)
```

## Limitaciones del MVP y siguientes pasos

- **Orientación, no asesoría legal.** El prompt obliga a citar y a no inventar, pero el
  modelo puede equivocarse. Verifica siempre en la fuente enlazada.
- **Cobertura**: solo SAT (Anexo 2 RMF 2026) y 9 leyes federales. Trámites como pasaporte,
  CURP, IMSS o INFONAVIT necesitan agregar sus fuentes (las páginas de gob.mx se indexan con
  `format: html`). Las leyes cambian: programa `ingest --refresh` periódicamente.
- **Latencia**: con el modelo de 27B, el primer token tarda ~10-20 s, la mayor parte en
  procesar el prompt con los fragmentos. Opciones: bajar `RAG_TOP_K`, fragmentos más chicos
  o un modelo MoE más rápido.
- **Demo sin cuentas:** usa datos ficticios. El servidor limita tamaño, concurrencia y
  frecuencia de peticiones; los documentos caducan a las 24 horas y no se cachean.
  Cualquiera que tenga el enlace de descarga puede usarlo hasta que caduque. El historial
  queda en el almacenamiento local del navegador. Para datos reales hacen falta
  autenticación, autorización por usuario y un aviso de privacidad acorde al servicio.
- La tarjeta de confirmación es la única salvaguarda antes de enviar datos personales al
  servicio externo. No la desactives (`CONFIRM_DOCUMENTS=false`) en producción.

## Contribuir

Proyecto abierto para **Hacktoberfest**: revisa [CONTRIBUTING.md](CONTRIBUTING.md) para preparar
el entorno e ideas de contribución (nuevas fuentes oficiales, plantillas de documentos,
evaluación de respuestas, accesibilidad, despliegue).

## Presentación

La presentación del proyecto para Hacktoberfest 2026 vive en
[`app/presentacion/`](app/presentacion/index.html) y la app la sirve junto al chat en
**`/presentacion/`** (en local: http://127.0.0.1:8700/presentacion/ tras `./scripts/dev.sh`).
Su diapositiva «Demo en vivo» muestra el chat real de la misma app.

Usa ← → para avanzar, `N` para las notas del presentador y `F` para pantalla completa.
Dentro de la demo en vivo, las flechas escriben en el chat: usa `Esc` y luego →, o
RePág / AvPág. Para exportarla a PDF, imprime desde Chrome con márgenes en "Ninguno".

## Licencia

[MIT](LICENSE). Los textos legales y fichas de trámite se descargan de sus fuentes oficiales al
indexar; no se redistribuyen en este repositorio.
