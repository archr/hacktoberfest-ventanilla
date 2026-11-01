"""API y frontend del chat ciudadano.

Ejecutar:  uvicorn app.main:app --port 8700
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .chat import ChatService, document_payload
from .documents.connector import DocumentConnector, DocumentServiceError, DocumentValidationError
from .documents.registry import DocumentRegistry
from .llm import OpenAICompatibleChat, OpenAICompatibleEmbedder
from .limits import RequestLimits, validate_document_data
from .rag.retriever import Retriever
from .rag.rewrite import QueryRewriter
from .rag.store import IndexStore
from .settings import Settings, get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("gobchat")
STATIC_DIR = Path(__file__).resolve().parent / "static"
PRESENTATION_DIR = Path(__file__).resolve().parent / "presentacion"

SUGGESTIONS = [
    "¿Cómo me inscribo en el RFC por primera vez?",
    "¿Qué necesito para tramitar mi e.firma en el SAT?",
    "¿Cuántos días de vacaciones me corresponden por ley?",
    "Quiero que me generes una carta poder para que alguien recoja mi e.firma",
]

# Dictámenes técnicos (app/dictamen/): oficio -> extracción con Qwen -> tarjeta -> DOCX
DICTAMEN_ACCEPT = ".pdf,.docx,.jpg,.jpeg,.png"
DICTAMEN_TYPE = "dictamen_tecnico"
DICTAMEN_TITLE = "Dictamen técnico"
DICTAMEN_MAX_TEXT = 500
UPLOAD_OVERHEAD = 64 * 1024  # encabezados multipart y conversation_id
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")  # inválidos en XML 1.0


def _load_dictamen(s: Settings) -> bool:
    """Los módulos se importan solo si la función está activa: la imagen de la demo pública
    puede no incluir app/dictamen/."""
    if not s.dictamen_enabled:
        return False
    try:
        from .dictamen import extract, generate, schema, tecnicos  # noqa: F401
    except ImportError:
        log.exception("DICTAMEN_ENABLED=true pero app/dictamen no está completo; se desactivan los dictámenes")
        return False
    if not s.dictamen_template_path.exists():
        log.error(
            "Falta la plantilla del dictamen (%s). Genérala con: python scripts/convert_dictamen_template.py",
            s.dictamen_template_path,
        )
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    registry = DocumentRegistry.from_file(s.documents_file)
    connector = DocumentConnector(
        registry, s.generated_dir, max_bytes=s.max_document_bytes,
        retention_hours=s.document_retention_hours,
    )
    await asyncio.to_thread(connector.purge_expired)
    llm = OpenAICompatibleChat(
        s.llm_base_url,
        s.llm_model,
        api_key=s.llm_api_key,
        temperature=s.llm_temperature,
        reasoning_effort=s.llm_reasoning_effort,
        timeout_s=s.llm_timeout_s,
    )
    app.state.dictamen_enabled = _load_dictamen(s)
    app.state.dictamen_extractor = None
    if app.state.dictamen_enabled:
        # OficioExtractor hace las llamadas a Qwen de una en una (el servidor local atiende en serie).
        from .dictamen.extract import OficioExtractor

        app.state.dictamen_extractor = OficioExtractor(
            s.llm_base_url,
            s.dictamen_model,
            api_key=s.llm_api_key,
            timeout_s=s.dictamen_extract_timeout_s,
            max_pages=s.dictamen_max_pages,
        )
    store = IndexStore(s.index_path)
    embedder = OpenAICompatibleEmbedder(
        s.effective_embed_base_url, s.embed_model, api_key=s.llm_api_key, query_instruction=s.embed_query_instruction
    )
    retriever = Retriever(store, embedder, candidates=s.rag_candidates, max_per_section=s.rag_max_per_section)
    if store.stats()["chunks"] == 0:
        log.warning("El índice está vacío. Ejecuta: python -m app.rag.ingest")

    app.state.settings = s
    app.state.store = store
    app.state.retriever = retriever
    app.state.registry = registry
    app.state.connector = connector
    app.state.chat = ChatService(
        llm,
        retriever,
        registry,
        connector,
        top_k=s.rag_top_k,
        max_history=s.max_history_messages,
        max_tool_rounds=s.max_tool_rounds,
        timezone=s.timezone,
        rewriter=QueryRewriter(llm) if s.rag_query_rewrite else None,
        confirm_documents=s.confirm_documents,
    )
    async def cleanup_documents():
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(connector.purge_expired)

    cleanup = asyncio.create_task(cleanup_documents())
    try:
        yield
    finally:
        cleanup.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup
        await llm.aclose()
        aclose = getattr(app.state.dictamen_extractor, "aclose", None)
        if aclose is not None:
            await aclose()
        store.close()


app = FastAPI(title="Ventanilla — chat de trámites y leyes (MVP)", version="0.1.0", lifespan=lifespan)
_settings = get_settings()
app.add_middleware(
    RequestLimits,
    max_bytes=_settings.max_request_bytes,
    concurrent=_settings.max_concurrent_requests,
    per_minute=_settings.requests_per_minute,
    # La subida del oficio tiene su propio tope; el resto de los POST sigue en max_request_bytes.
    route_limits={
        "/api/dictamen/oficio": (
            _settings.dictamen_max_upload_bytes + UPLOAD_OVERHEAD,
            f"El archivo supera el máximo de {_settings.dictamen_max_upload_mb:g} MB.",
        ),
    },
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Presentación del proyecto; su diapositiva "Demo en vivo" muestra el chat de esta misma app.
app.mount("/presentacion", StaticFiles(directory=PRESENTATION_DIR, html=True), name="presentacion")


class ChatRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(..., max_length=200)
    conversation_id: str | None = Field(default=None, max_length=64)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    # sin caché: así los cambios de versión de app.js/styles.css (?v=N) llegan al navegador
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
def config(request: Request) -> dict:
    st = request.app.state
    return {
        "model": st.settings.llm_model,
        "documents": st.registry.summary(),
        "suggestions": SUGGESTIONS,
        "index": st.store.stats(),
        "demo": st.settings.demo_mode,
        "document_retention_hours": st.settings.document_retention_hours,
        "dictamen": _dictamen_config(st) if getattr(st, "dictamen_enabled", False) else None,
    }


@app.get("/api/sources")
def sources(request: Request) -> dict:
    return {"sources": request.app.state.store.list_documents()}


@app.get("/healthz", include_in_schema=False)
async def liveness() -> dict:
    # El reinicio del contenedor no debe depender de que la Mac Studio esté encendida.
    return {"status": "ok"}


@app.get("/api/health")
async def health(request: Request) -> dict:
    st = request.app.state
    llm_ok, detail = False, ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(st.settings.llm_base_url.rstrip("/") + "/models")
            llm_ok = r.status_code == 200
            models = [m.get("id") for m in r.json().get("data", [])] if llm_ok else []
            if llm_ok and st.settings.llm_model not in models:
                llm_ok, detail = False, f"Modelo {st.settings.llm_model} no encontrado"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
    return {
        "llm": {"ok": llm_ok, "model": st.settings.llm_model, "detail": detail},
        "index": st.store.stats(),
        "vectors": st.retriever.has_vectors,
        "documents": [d["id"] for d in st.registry.summary()],
        "dictamen": {
            "enabled": bool(getattr(st, "dictamen_enabled", False)),
            "template": st.settings.dictamen_template_path.exists(),
        },
    }


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    service: ChatService = request.app.state.chat
    conversation_id = req.conversation_id or uuid.uuid4().hex

    async def stream():
        yield _sse("meta", {"conversation_id": conversation_id})
        try:
            async for event in service.run(req.messages, conversation_id):
                yield _sse(event["event"], event["data"])
        except Exception:  # noqa: BLE001
            log.exception("Error inesperado en el chat")
            yield _sse("error", {"message": "Ocurrió un error inesperado. Intenta de nuevo."})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class GenerateRequest(BaseModel):
    type: str = Field(..., max_length=64)
    data: dict[str, Any]
    conversation_id: str | None = Field(default=None, max_length=64)

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value):
        return validate_document_data(value)


@app.post("/api/documents")
async def generate_document(req: GenerateRequest, request: Request):
    """Genera un documento con los datos que la persona confirmó en la tarjeta."""
    st = request.app.state
    doc_type = st.registry.by_tool(f"generar_{req.type}")
    if doc_type is None:
        raise HTTPException(status_code=404, detail="Tipo de documento desconocido")
    data = {k: v for k, v in req.data.items() if v not in (None, "", [])}
    try:
        doc = await st.connector.generate(doc_type, data, req.conversation_id)
    except DocumentValidationError as exc:
        return JSONResponse(status_code=422, content={"message": "Revisa los datos marcados.", "errors": exc.errors})
    except DocumentServiceError as exc:
        log.warning("Servicio de documentos: %s", exc)
        return JSONResponse(status_code=502, content={"message": str(exc)})
    return document_payload(doc)


@app.get("/api/documents/{doc_id}")
def download(doc_id: str, request: Request) -> FileResponse:
    found = request.app.state.connector.get(doc_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    path, meta = found
    return FileResponse(
        path, media_type=meta.mime_type, filename=meta.filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------- dictámenes técnicos ----------
def _dictamen_state(request: Request):
    st = request.app.state
    if not getattr(st, "dictamen_enabled", False):
        raise HTTPException(status_code=404, detail="Los dictámenes no están habilitados en esta instalación")
    return st


def _tecnicos(s: Settings) -> list[dict]:
    from .dictamen.tecnicos import cargar_tecnicos

    try:
        return list(cargar_tecnicos(s.dictamen_tecnicos_path) or [])
    except (OSError, ValueError) as exc:
        log.warning("No se pudo leer el catálogo de técnicos: %s", exc)
        return []


def _dictamen_config(st) -> dict:
    from .dictamen.schema import MAX_EQUIPOS, field_spec

    s = st.settings
    return {
        "enabled": True,
        "fields": field_spec(),
        "tecnicos": [
            {"nombre": str(t.get("nombre") or ""), "iniciales": str(t.get("iniciales") or "")}
            for t in _tecnicos(s)
            if isinstance(t, dict)
        ],
        "max_upload_mb": s.dictamen_max_upload_mb,
        "max_equipos": MAX_EQUIPOS,
        "accept": DICTAMEN_ACCEPT,
    }


@app.get("/api/dictamen/config")
def dictamen_config(request: Request) -> dict:
    return _dictamen_config(_dictamen_state(request))


def _display_name(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:120] or "oficio"


def _draft_response(schema, message: str, filename: str) -> dict:
    draft = schema.empty_draft()
    return {"message": message, "filename": filename, **draft, "faltantes": schema.faltantes(draft["data"], draft["own"])}


@app.post("/api/dictamen/oficio")
async def dictamen_oficio(
    request: Request,
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None, max_length=64),
):
    """Lee un oficio (PDF, DOCX o foto) con el LLM y devuelve los datos para la tarjeta."""
    st = _dictamen_state(request)
    from .dictamen import extract, schema

    s: Settings = st.settings
    limit = s.dictamen_max_upload_bytes
    content = await file.read(limit + 1)
    filename = _display_name(file.filename)
    if len(content) > limit:
        return JSONResponse(status_code=413, content={"message": f"El archivo supera el máximo de {s.dictamen_max_upload_mb:g} MB."})
    if not content:
        return JSONResponse(status_code=415, content={"message": "El archivo está vacío."})
    if extract.sniff_mime(content, filename) is None:
        return JSONResponse(
            status_code=415,
            content={"message": "Formato no reconocido. Adjunta el oficio en PDF, Word (.docx) o foto JPG/PNG."},
        )
    try:
        result = await st.dictamen_extractor.extract(content, filename) or {}
        data = schema.normalize_extraction(result.get("data"))
    except extract.ExtractionError as exc:
        log.warning("No se pudo extraer el oficio: %s", exc)
        return JSONResponse(status_code=502, content=_draft_response(schema, str(exc), filename))
    except Exception:  # noqa: BLE001
        log.exception("Error inesperado al leer el oficio")
        return JSONResponse(
            status_code=502,
            content=_draft_response(schema, "Ocurrió un error inesperado al leer el oficio.", filename),
        )
    own = schema.empty_draft()["own"]
    return {
        "filename": filename,
        "data": data,
        "own": own,
        "faltantes": schema.faltantes(data, own),
        "mode": result.get("mode"),
        "pages": result.get("pages"),
        "total_pages": result.get("total_pages", result.get("pages")),
        "elapsed_s": result.get("elapsed_s"),
    }


class DictamenRequest(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    own: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = Field(default=None, max_length=64)


class _InputErrors(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _dictamen_input(raw_data: dict, raw_own: dict, schema) -> tuple[dict, dict]:
    """Lista blanca de campos, tipos y longitudes; nunca se aceptan claves desconocidas."""
    errors: list[str] = []

    def text(value, label):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            errors.append(f"{label}: debe ser texto")
            return None
        value = str(value)
        if len(value) > DICTAMEN_MAX_TEXT:
            errors.append(f"{label}: admite hasta {DICTAMEN_MAX_TEXT} caracteres")
            return None
        return value

    data: dict[str, Any] = {f["name"]: text(raw_data.get(f["name"]), f["label"]) for f in schema.GENERAL_FIELDS}
    equipos = raw_data.get("equipos")
    if equipos is None:
        equipos = []
    if not isinstance(equipos, list):
        errors.append("Equipos: debe ser una lista")
        equipos = []
    elif len(equipos) > schema.MAX_EQUIPOS:
        errors.append(f"Se admiten hasta {schema.MAX_EQUIPOS} equipos por dictamen")
        equipos = []
    data["equipos"] = []
    for i, eq in enumerate(equipos, 1):
        if not isinstance(eq, dict):
            errors.append(f"Equipo {i}: formato inválido")
            continue
        data["equipos"].append(
            {f["name"]: text(eq.get(f["name"]), f"Equipo {i} · {f['label']}") for f in schema.EQUIPO_FIELDS}
        )
    own = {}
    for f in schema.OWN_FIELDS:
        value = text(raw_own.get(f["name"]), f["label"])
        value = " ".join(_XML_INVALID.sub("", value).split()) if value else ""
        own[f["name"]] = value or None
    if errors:
        raise _InputErrors(errors)
    return schema.normalize_extraction(data), own


def _faltantes_text(names: list[str], schema) -> str:
    labels = {f["name"]: f["label"] for f in [*schema.GENERAL_FIELDS, *schema.OWN_FIELDS]}
    eq_labels = {f["name"]: f["label"] for f in schema.EQUIPO_FIELDS}
    out = []
    for name in names:
        m = re.fullmatch(r"equipo_(\d+)_(.+)", name)
        if name == "equipos":
            out.append("al menos un equipo")
        elif m:
            out.append(f"equipo {m.group(1)}: {eq_labels.get(m.group(2), m.group(2))}")
        elif name == "dictamen_inicial":
            out.append(f"{labels.get(name, name)} (solo números)")
        else:
            out.append(labels.get(name, name))
    shown = ", ".join(out[:8]) + (f" y {len(out) - 8} más" if len(out) > 8 else "")
    return f"Faltan datos: {shown}."


def _safe_numero(numero: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "_", numero.upper()).strip("._-")[:60] or "sin_numero"


@app.post("/api/dictamen/generar")
async def dictamen_generar(req: DictamenRequest, request: Request):
    """Genera el DOCX del dictamen con los datos que la persona revisó en la tarjeta."""
    st = _dictamen_state(request)
    from .dictamen import extract, generate, schema, tecnicos

    s: Settings = st.settings
    try:
        data, own = _dictamen_input(req.data, req.own, schema)
    except _InputErrors as exc:
        return JSONResponse(status_code=422, content={"message": "Revisa los datos marcados.", "errors": exc.errors})

    if own.get("iniciales"):
        resolved = tecnicos.resolver_iniciales(own["iniciales"], _tecnicos(s))
        if "error" in resolved:
            return JSONResponse(
                status_code=422, content={"message": resolved["error"], "errors": [resolved["error"]], "field": "iniciales"}
            )
        own["iniciales"] = resolved["valor"]

    missing = schema.faltantes(data, own)
    if missing:
        return JSONResponse(status_code=422, content={"message": _faltantes_text(missing, schema), "faltantes": missing})

    # El dictamen lleva la fecha de Oaxaca (America/Mexico_City), no la del servidor.
    today = datetime.now(ZoneInfo(s.timezone)).date()
    try:
        context = generate.build_context(data, own, today)
    except generate.DictamenError as exc:
        return JSONResponse(
            status_code=422,
            content={"message": str(exc), "errors": [str(exc)], "faltantes": list(getattr(exc, "faltantes", []))},
        )
    if not s.dictamen_template_path.exists():
        log.error("Falta la plantilla del dictamen: %s", s.dictamen_template_path)
        return JSONResponse(
            status_code=503,
            content={"message": "Falta la plantilla del dictamen en el servidor. Avisa a la persona administradora."},
        )
    plantilla_error = getattr(generate, "PlantillaError", ())
    try:
        content = await asyncio.to_thread(generate.render_dictamen, data, own, s.dictamen_template_path, today)
        filename = f"dictamen_{_safe_numero(own['numero_oficio'])}_{today.isoformat()}.docx"
        doc = st.connector.store_file(DICTAMEN_TYPE, DICTAMEN_TITLE, content, extract.DOCX_MIME, filename)
    except generate.DictamenError as exc:
        return JSONResponse(status_code=422, content={"message": str(exc), "errors": [str(exc)]})
    except plantilla_error:
        log.exception("La plantilla del dictamen falló")
        return JSONResponse(
            status_code=500,
            content={"message": "La plantilla del dictamen tiene un problema en el servidor. Avisa a la persona administradora."},
        )
    except Exception:  # noqa: BLE001
        log.exception("No se pudo generar el dictamen")
        return JSONResponse(status_code=500, content={"message": "No se pudo generar el dictamen por un error interno."})

    equipos = context["equipos"]
    log.info("Dictamen generado: %d equipo(s)", len(equipos))
    return {
        **document_payload(doc),
        "iniciales": own["iniciales"],
        # como quedan impresos (con ceros a la izquierda si la persona los escribió)
        "numeros": [equipos[0]["num_dictamen"], equipos[-1]["num_dictamen"]],
    }

