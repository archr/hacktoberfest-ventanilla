import json

import httpx
import pymupdf
import pytest

from app.documents.connector import DocumentConnector, DocumentServiceError, DocumentValidationError
from mock_doc_service.templates import TEMPLATES

from .conftest import CARTA


def test_registry_builds_one_tool_per_document(registry):
    names = [t["function"]["name"] for t in registry.tools()]
    assert names == ["generar_carta_poder", "generar_escrito_libre_sat", "generar_solicitud_acceso_informacion"]
    assert registry.services["generador"].base_url == "http://docs.test"
    assert registry.services["generador"].auth_headers() == {}
    assert "SOLO" in registry.tools()[0]["function"]["description"]


def test_bearer_token_from_env(registry, monkeypatch):
    monkeypatch.setenv("DOC_SERVICE_TOKEN", "secreto")
    assert registry.services["generador"].auth_headers() == {"Authorization": "Bearer secreto"}


def test_validation_reports_missing_and_blank_fields(registry):
    doc = registry.by_tool("generar_carta_poder")
    errors = doc.validate({**CARTA, "testigo2_nombre": "  ", "otorgante_nombre": None} | {"extra": 1})
    joined = " ".join(errors)
    assert "testigo2_nombre" in joined and "otorgante_nombre" in joined
    assert "extra" in joined  # additionalProperties: false


@pytest.mark.parametrize("doc_type", sorted(TEMPLATES))
def test_templates_render_spanish_text(doc_type):
    data = {k: "Ñandú «Pérez» — acción" for k in ["otorgante_nombre", "apoderado_nombre", "dependencia", "tramite",
            "testigo1_nombre", "testigo2_nombre", "lugar", "nombre", "rfc", "domicilio_fiscal", "correo_electronico",
            "autoridad", "asunto", "hechos", "peticion", "sujeto_obligado", "descripcion_informacion",
            "modalidad_entrega", "medio_notificacion"]}
    pdf = TEMPLATES[doc_type](data)
    text = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
    assert pdf.startswith(b"%PDF") and "Ñandú" in text


async def test_connector_generates_and_stores_pdf(connector, registry):
    doc = await connector.generate(registry.by_tool("generar_carta_poder"), CARTA, "conv1")
    assert doc.mime_type == "application/pdf" and doc.filename.startswith("carta_poder_")
    path, meta = connector.get(doc.id)
    text = pymupdf.open(path)[0].get_text()
    assert "Juan Pérez López" in text and "recoger el certificado de e.firma" in text
    assert meta.title == "Carta poder simple"


async def test_connector_rejects_invalid_data_without_calling_service(registry, tmp_path):
    def boom(request):
        raise AssertionError("no debe llamarse")

    conn = DocumentConnector(registry, tmp_path, transport=httpx.MockTransport(boom))
    with pytest.raises(DocumentValidationError):
        await conn.generate(registry.by_tool("generar_carta_poder"), {"otorgante_nombre": "Juan"})


async def test_connector_accepts_binary_response_and_polling(registry, tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request):
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["type"] == "carta_poder" and body["data"]["lugar"] == CARTA["lugar"]
            return httpx.Response(202, json={"status": "processing", "status_url": "/v1/jobs/1"})
        if request.url.path == "/v1/jobs/1":
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(200, json={"status": "processing", "status_url": "/v1/jobs/1"})
            return httpx.Response(200, json={"status": "ready", "download_url": "/files/1"})
        return httpx.Response(200, content=b"%PDF-1.4 fake", headers={"content-type": "application/pdf"})

    conn = DocumentConnector(registry, tmp_path, transport=httpx.MockTransport(handler), poll_interval_s=0)
    doc = await conn.generate(registry.by_tool("generar_carta_poder"), CARTA)
    assert doc.size == len(b"%PDF-1.4 fake") and calls["n"] == 2


async def test_credentials_not_sent_to_other_origins(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_SERVICE_TOKEN", "secreto")
    seen = {}

    def handler(request: httpx.Request):
        seen[request.url.host] = request.headers.get("authorization")
        if request.url.host == "docs.test":
            return httpx.Response(200, json={"status": "ready", "download_url": "https://bucket.example/f.pdf?sig=1"})
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

    conn = DocumentConnector(registry, tmp_path, transport=httpx.MockTransport(handler))
    await conn.generate(registry.by_tool("generar_carta_poder"), CARTA)
    assert seen == {"docs.test": "Bearer secreto", "bucket.example": None}


async def test_service_errors_are_wrapped(registry, tmp_path):
    conn = DocumentConnector(registry, tmp_path, transport=httpx.MockTransport(lambda r: httpx.Response(500, text="caído")))
    with pytest.raises(DocumentServiceError, match="500"):
        await conn.generate(registry.by_tool("generar_carta_poder"), CARTA)


def test_get_rejects_path_traversal(connector):
    assert connector.get("../../etc/passwd") is None
