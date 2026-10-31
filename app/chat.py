"""Orquestación de un turno de chat: RAG → LLM (stream) → herramientas → respuesta."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .documents.connector import DocumentConnector, DocumentServiceError, DocumentValidationError
from .documents.registry import DocumentRegistry
from .llm import ChatModel, LLMError, ToolCall
from .prompts import CONTEXT_TEMPLATE, SYSTEM_PROMPT, format_sources
from .rag.rewrite import QueryRewriter
from .rag.store import ChunkRecord

log = logging.getLogger(__name__)

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]


class SearchBackend(Protocol):
    async def search(self, question: str | list[str], k: int = 6) -> list[ChunkRecord]: ...


Event = dict[str, Any]  # {"event": str, "data": dict}


def sanitize_history(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Acepta solo roles user/assistant/tool y recorta conservando turnos completos."""
    clean: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            clean.append({"role": "user", "content": str(m.get("content") or "")[:8000]})
        elif role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": str(m.get("content") or "")}
            if m.get("tool_calls"):
                msg["tool_calls"] = m["tool_calls"]
            clean.append(msg)
        elif role == "tool" and m.get("tool_call_id"):
            clean.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": str(m.get("content") or "")})
    clean = clean[-limit:]
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    return clean


def retrieval_query(history: list[dict[str, Any]]) -> str:
    users = [m["content"] for m in history if m["role"] == "user"]
    if not users:
        return ""
    last = users[-1]
    # En respuestas cortas ("sí", "persona física") la pregunta previa da el contexto.
    if len(last.split()) < 8 and len(users) > 1:
        return f"{users[-2]}\n{last}"
    return last


def source_payload(chunks: list[ChunkRecord]) -> list[dict[str, Any]]:
    return [
        {
            "n": i,
            "heading": ch.heading,
            "document": ch.doc_title,
            "institution": ch.institution,
            "version": ch.version,
            "page": ch.page,
            "url": ch.link,
            "snippet": ch.text[:280],
        }
        for i, ch in enumerate(chunks, 1)
    ]


class ChatService:
    def __init__(
        self,
        llm: ChatModel,
        retriever: SearchBackend | None,
        registry: DocumentRegistry,
        connector: DocumentConnector,
        top_k: int = 6,
        max_history: int = 20,
        max_tool_rounds: int = 3,
        timezone: str = "America/Mexico_City",
        rewriter: QueryRewriter | None = None,
        confirm_documents: bool = True,
    ) -> None:
        self.llm = llm
        self.rewriter = rewriter
        self.confirm_documents = confirm_documents
        self.retriever = retriever
        self.registry = registry
        self.connector = connector
        self.top_k = top_k
        self.max_history = max_history
        self.max_tool_rounds = max_tool_rounds
        self.tz = ZoneInfo(timezone)

    def system_prompt(self) -> str:
        now = datetime.now(self.tz)
        today = f"{DIAS[now.weekday()]} {now.day} de {MESES[now.month - 1]} de {now.year} ({now.date().isoformat()})"
        docs = ", ".join(f"«{d.title}»" for d in self.registry.documents) or "ninguno"
        return SYSTEM_PROMPT.format(today=today, documents=docs)

    async def run(self, raw_history: list[dict[str, Any]], conversation_id: str | None = None) -> AsyncIterator[Event]:
        history = sanitize_history(raw_history, self.max_history)
        if not history or history[-1]["role"] != "user":
            yield {"event": "error", "data": {"message": "El último mensaje debe ser de la persona usuaria."}}
            return

        chunks: list[ChunkRecord] = []
        if self.retriever is not None:
            queries = [retrieval_query(history)]
            if self.rewriter is not None:
                rewritten = await self.rewriter.rewrite(history)
                if rewritten:
                    queries.append(rewritten)
                    yield {"event": "search", "data": {"query": rewritten}}
            try:
                chunks = await self.retriever.search(queries, k=self.top_k)
            except Exception as exc:  # noqa: BLE001
                log.exception("Fallo la búsqueda")
                yield {"event": "warning", "data": {"message": f"No se pudo consultar la base de conocimiento: {exc}"}}
        yield {"event": "sources", "data": {"sources": source_payload(chunks)}}

        question = history[-1]["content"]
        llm_messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()},
            *history[:-1],
            {"role": "user", "content": CONTEXT_TEMPLATE.format(question=question, sources=format_sources(chunks))},
        ]
        tools = self.registry.tools() or None
        new_messages: list[dict[str, Any]] = []

        streamed_text = False
        for round_ in range(self.max_tool_rounds + 1):
            content_parts: list[str] = []
            calls: list[ToolCall] = []
            try:
                async for chunk in self.llm.stream_chat(llm_messages, tools=tools if round_ < self.max_tool_rounds else None):
                    if chunk.kind == "reasoning":
                        yield {"event": "reasoning", "data": {"text": chunk.text}}
                    elif chunk.kind == "content":
                        if not content_parts and streamed_text:
                            # separa visualmente el texto de una ronda anterior (antes de la herramienta)
                            yield {"event": "token", "data": {"text": "\n\n"}}
                        content_parts.append(chunk.text)
                        streamed_text = True
                        yield {"event": "token", "data": {"text": chunk.text}}
                    elif chunk.kind == "tool_calls":
                        calls = chunk.tool_calls
            except LLMError as exc:
                log.error("LLM: %s", exc)
                yield {"event": "error", "data": {"message": str(exc)}}
                return

            content = "".join(content_parts)
            if not calls:
                new_messages.append({"role": "assistant", "content": content})
                break

            assistant_msg = {"role": "assistant", "content": content, "tool_calls": [c.to_message() for c in calls]}
            llm_messages.append(assistant_msg)
            new_messages.append(assistant_msg)
            for call in calls:
                result = None
                async for event in self._run_tool(call, conversation_id):
                    if event["event"] == "_result":
                        result = event["data"]
                    else:
                        yield event
                tool_msg = {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, ensure_ascii=False)}
                llm_messages.append(tool_msg)
                new_messages.append(tool_msg)

        yield {"event": "done", "data": {"messages": new_messages}}

    async def _run_tool(self, call: ToolCall, conversation_id: str | None) -> AsyncIterator[Event]:
        doc_type = self.registry.by_tool(call.name)
        if doc_type is None:
            yield {"event": "_result", "data": {"ok": False, "error": f"Herramienta desconocida: {call.name}"}}
            return
        try:
            args = call.parsed_arguments()
        except ValueError as exc:
            yield {"event": "_result", "data": {"ok": False, "error": str(exc)}}
            return
        args = {k: v for k, v in args.items() if v not in (None, "", [])}
        errors = doc_type.validate(args)
        if errors:
            # Se devuelve al modelo para que pida los datos que faltan.
            yield {"event": "_result", "data": {"ok": False, "error": "Datos incompletos o inválidos", "detalles": errors}}
            return

        if self.confirm_documents:
            # Humano en el ciclo: la persona revisa/corrige los datos en una tarjeta y
            # solo al confirmar se envían al servicio externo (POST /api/documents).
            yield {
                "event": "confirm_document",
                "data": {
                    "call_id": call.id,
                    "type": doc_type.id,
                    "title": doc_type.title,
                    "fields": doc_type.fields(),
                    "data": args,
                },
            }
            yield {
                "event": "_result",
                "data": {
                    "ok": True,
                    "estado": "pendiente_de_confirmacion",
                    "mensaje": (
                        "Se mostró a la persona una tarjeta con los datos para revisarlos, corregirlos y "
                        "presionar «Generar documento». Dile brevemente que revise la tarjeta; no repitas "
                        "todos los datos ni pidas confirmación por chat."
                    ),
                },
            }
            return

        yield {"event": "tool_start", "data": {"tool": call.name, "document_type": doc_type.id, "title": doc_type.title}}
        try:
            doc = await self.connector.generate(doc_type, args, conversation_id)
        except (DocumentServiceError, DocumentValidationError) as exc:
            log.warning("Documento %s falló: %s", doc_type.id, exc)
            yield {"event": "tool_error", "data": {"title": doc_type.title, "message": str(exc)}}
            yield {"event": "_result", "data": {"ok": False, "error": str(exc)}}
            return
        yield {"event": "document", "data": document_payload(doc)}
        yield {
            "event": "_result",
            "data": {
                "ok": True,
                "documento": doc.title,
                "archivo": doc.filename,
                "mensaje": "Documento generado. La persona ya puede descargarlo desde la tarjeta que aparece en el chat.",
            },
        }


def document_payload(doc) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "filename": doc.filename,
        "mime_type": doc.mime_type,
        "size": doc.size,
        "url": doc.download_path,
    }
