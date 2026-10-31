from pathlib import Path

import httpx
import pytest

import mock_doc_service.main as mock_main
from app.documents.connector import DocumentConnector
from app.documents.registry import DocumentRegistry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setenv("DOC_SERVICE_URL", "http://docs.test")
    monkeypatch.delenv("DOC_SERVICE_TOKEN", raising=False)
    return DocumentRegistry.from_file(ROOT / "config" / "document_services.yaml")


@pytest.fixture
def mock_service(tmp_path, monkeypatch):
    monkeypatch.setattr(mock_main, "STORAGE", tmp_path / "mock_docs")
    return httpx.ASGITransport(app=mock_main.app)


@pytest.fixture
def connector(registry, mock_service, tmp_path):
    return DocumentConnector(registry, tmp_path / "generated", transport=mock_service)


CARTA = {
    "otorgante_nombre": "Juan Pérez López",
    "apoderado_nombre": "María López Ruiz",
    "dependencia": "Servicio de Administración Tributaria (SAT)",
    "tramite": "recoger el certificado de e.firma",
    "testigo1_nombre": "Ana Gómez",
    "testigo2_nombre": "Luis Núñez",
    "lugar": "Guadalajara, Jalisco",
    "fecha": "2026-10-31",
}
