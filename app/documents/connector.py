"""Conector HTTP genérico hacia servicios de generación de documentos.

El archivo resultante siempre se descarga y se guarda localmente, para que el
navegador lo obtenga desde esta app (/api/documents/{id}) aunque el servicio
externo use URLs internas, firmadas o con expiración.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .registry import DocumentRegistry, DocumentType

log = logging.getLogger(__name__)

_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_NAME = re.compile(r"[^\w\-. ]+", re.UNICODE)
_FILE_TYPES = ("application/pdf", "application/octet-stream", "application/vnd.", "application/msword")


class DocumentServiceError(RuntimeError):
    """El servicio externo falló o no está disponible."""


class DocumentValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class GeneratedDocument:
    id: str
    type: str
    title: str
    filename: str
    mime_type: str
    size: int
    created_at: str

    @property
    def download_path(self) -> str:
        return f"/api/documents/{self.id}"


class DocumentConnector:
    def __init__(
        self,
        registry: DocumentRegistry,
        storage_dir: Path,
        max_bytes: int = 20 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 1.0,
        retention_hours: float = 24,
    ) -> None:
        self.registry = registry
        self.storage_dir = storage_dir
        self.max_bytes = max_bytes
        self.transport = transport
        self.poll_interval_s = poll_interval_s
        self.retention_seconds = retention_hours * 3600
        storage_dir.mkdir(parents=True, exist_ok=True)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=self.transport,
            follow_redirects=False,
        )

    async def generate(
        self, doc: DocumentType, data: dict[str, Any], conversation_id: str | None = None
    ) -> GeneratedDocument:
        errors = doc.validate(data)
        if errors:
            raise DocumentValidationError(errors)

        service = self.registry.services[doc.service]
        url = service.base_url + doc.path
        payload = {
            "type": doc.id,
            "data": data,
            "metadata": {
                "conversation_id": conversation_id,
                "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": "gobchat",
            },
        }
        headers = {"Accept": "application/json, application/pdf, */*", **service.auth_headers()}
        log.info("Generando documento '%s' en servicio '%s'", doc.id, service.name)
        try:
            async with self._client(service.timeout_s) as client:
                resp = await client.request(doc.method, url, json=payload, headers=headers)
                content, mime, filename = await self._resolve(client, resp, service.base_url, headers, service.timeout_s)
        except httpx.HTTPError as exc:
            raise DocumentServiceError(f"No se pudo contactar el servicio de documentos: {exc}") from exc

        return self._store(doc, content, mime, filename)

    async def _resolve(
        self,
        client: httpx.AsyncClient,
        resp: httpx.Response,
        base_url: str,
        headers: dict[str, str],
        timeout_s: float,
    ) -> tuple[bytes, str, str | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if resp.status_code >= 400:
                raise DocumentServiceError(
                    f"El servicio de documentos respondió {resp.status_code}: {resp.text[:300]}"
                )
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype.startswith(_FILE_TYPES):
                return self._check_size(resp.content), ctype, _filename_from_headers(resp)
            if ctype != "application/json":
                raise DocumentServiceError(f"Respuesta inesperada del servicio ({ctype or 'sin tipo'})")

            body = resp.json()
            status = str(body.get("status", "ready")).lower()
            if status in {"error", "failed"}:
                raise DocumentServiceError(body.get("error") or "El servicio no pudo generar el documento")
            if status in {"pending", "processing", "queued"}:
                status_url = body.get("status_url")
                if not status_url or loop.time() > deadline:
                    raise DocumentServiceError("El documento sigue en proceso; inténtalo más tarde")
                await asyncio.sleep(self.poll_interval_s)
                poll_url = urljoin(base_url + "/", status_url)
                if _origin(poll_url) != _origin(base_url):
                    raise DocumentServiceError("status_url debe pertenecer al mismo servicio")
                resp = await client.get(poll_url, headers=headers)
                continue

            filename = body.get("filename")
            mime = body.get("mime_type") or "application/pdf"
            if body.get("content_base64"):
                return self._check_size(base64.b64decode(body["content_base64"])), mime, filename
            if body.get("download_url"):
                file_resp = await _download(client, urljoin(base_url + "/", body["download_url"]), base_url, headers)
                if file_resp.status_code >= 400:
                    raise DocumentServiceError(f"No se pudo descargar el documento ({file_resp.status_code})")
                file_mime = file_resp.headers.get("content-type", mime).split(";")[0].strip()
                return self._check_size(file_resp.content), file_mime or mime, filename
            raise DocumentServiceError("La respuesta del servicio no incluye download_url ni content_base64")

    def _check_size(self, content: bytes) -> bytes:
        if len(content) > self.max_bytes:
            raise DocumentServiceError("El documento excede el tamaño máximo permitido")
        if not content:
            raise DocumentServiceError("El servicio devolvió un documento vacío")
        return content

    def _store(self, doc: DocumentType, content: bytes, mime: str, filename: str | None) -> GeneratedDocument:
        doc_id = uuid.uuid4().hex
        ext = mimetypes.guess_extension(mime) or Path(filename or "").suffix or ".bin"
        if not filename:
            filename = f"{doc.id}{ext}"
        filename = _SAFE_NAME.sub("_", Path(filename).name)[:120] or f"{doc.id}{ext}"
        (self.storage_dir / f"{doc_id}.bin").write_bytes(content)
        meta = GeneratedDocument(
            id=doc_id,
            type=doc.id,
            title=doc.title,
            filename=filename,
            mime_type=mime,
            size=len(content),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        (self.storage_dir / f"{doc_id}.json").write_text(json.dumps(asdict(meta), ensure_ascii=False))
        return meta

    def get(self, doc_id: str) -> tuple[Path, GeneratedDocument] | None:
        if not _ID.match(doc_id):
            return None
        meta_path = self.storage_dir / f"{doc_id}.json"
        file_path = self.storage_dir / f"{doc_id}.bin"
        if not (meta_path.exists() and file_path.exists()):
            return None
        if meta_path.stat().st_mtime < time.time() - self.retention_seconds:
            meta_path.unlink(missing_ok=True)
            file_path.unlink(missing_ok=True)
            return None
        return file_path, GeneratedDocument(**json.loads(meta_path.read_text()))

    def purge_expired(self) -> None:
        cutoff = time.time() - self.retention_seconds
        for path in self.storage_dir.iterdir():
            if path.suffix not in {".bin", ".json"} or not _ID.fullmatch(path.stem):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass  # Una descarga puede haber eliminado el archivo al vencer.


def _origin(url: str) -> tuple[str, str, int | None]:
    u = httpx.URL(url)
    return (u.scheme, u.host, u.port)


async def _download(
    client: httpx.AsyncClient, url: str, base_url: str, headers: dict[str, str]
) -> httpx.Response:
    """Descarga el archivo. Las credenciales solo se envían al mismo origen del servicio;
    las redirecciones (p. ej. a una URL firmada) se siguen sin credenciales."""
    same_origin = _origin(url) == _origin(base_url)
    req_headers = headers if same_origin else {"Accept": headers.get("Accept", "*/*")}
    resp = await client.get(url, headers=req_headers, follow_redirects=False)
    hops = 0
    while resp.is_redirect and hops < 5:
        location = urljoin(str(resp.url), resp.headers["location"])
        resp = await client.get(location, headers={"Accept": "*/*"}, follow_redirects=False)
        hops += 1
    return resp


def _filename_from_headers(resp: httpx.Response) -> str | None:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    return m.group(1) if m else None
