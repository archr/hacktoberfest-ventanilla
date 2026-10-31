"""Servicio SIMULADO de generación de documentos.

Implementa el mismo contrato que debe cumplir el servicio real (ver README).
Ejecutar:  uvicorn mock_doc_service.main:app --port 8701
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .templates import TEMPLATES

STORAGE = Path(os.environ.get("MOCK_DOC_STORAGE", Path(__file__).resolve().parent.parent / "data" / "mock_docs"))
_ID = re.compile(r"^[0-9a-f]{32}$")

app = FastAPI(title="Servicio simulado de documentos", version="0.1.0")


class DocumentRequest(BaseModel):
    type: str
    data: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


def _check_auth(authorization: str | None) -> None:
    token = os.environ.get("MOCK_DOC_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Token inválido")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "types": sorted(TEMPLATES)}


@app.post("/v1/documents")
def create_document(req: DocumentRequest, authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    render = TEMPLATES.get(req.type)
    if render is None:
        raise HTTPException(status_code=422, detail=f"Tipo de documento no soportado: {req.type}")
    content = render(req.data)
    STORAGE.mkdir(parents=True, exist_ok=True)
    doc_id = uuid.uuid4().hex
    (STORAGE / f"{doc_id}.pdf").write_bytes(content)
    return {
        "document_id": doc_id,
        "status": "ready",
        "download_url": f"/v1/documents/{doc_id}/file",
        "filename": f"{req.type}_{date.today().isoformat()}.pdf",
        "mime_type": "application/pdf",
    }


@app.get("/v1/documents/{doc_id}/file")
def get_file(doc_id: str, authorization: str | None = Header(default=None)) -> FileResponse:
    _check_auth(authorization)
    path = STORAGE / f"{doc_id}.pdf"
    if not _ID.match(doc_id) or not path.exists():
        raise HTTPException(status_code=404, detail="No encontrado")
    return FileResponse(path, media_type="application/pdf")
