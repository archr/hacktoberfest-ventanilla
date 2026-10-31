"""API y frontend del chat ciudadano.

Ejecutar:  uvicorn app.main:app --port 8700
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
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
from .settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("gobchat")
STATIC_DIR = Path(__file__).resolve().parent / "static"

SUGGESTIONS = [
    "¿Cómo me inscribo en el RFC por primera vez?",
    "¿Qué necesito para tramitar mi e.firma en el SAT?",
    "¿Cuántos días de vacaciones me corresponden por ley?",
    "Quiero que me generes una carta poder para que alguien recoja mi e.firma",
]


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
        store.close()


app = FastAPI(title="Ventanilla — chat de trámites y leyes (MVP)", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    RequestLimits,
    max_bytes=get_settings().max_request_bytes,
    concurrent=get_settings().max_concurrent_requests,
    per_minute=get_settings().requests_per_minute,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
