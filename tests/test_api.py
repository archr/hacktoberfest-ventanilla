import pymupdf
from fastapi.testclient import TestClient

from app.main import app

from .conftest import CARTA


def test_generate_and_download_document(connector):
    with TestClient(app) as client:
        client.app.state.connector = connector  # servicio simulado en memoria
        r = client.post("/api/documents", json={"type": "carta_poder", "data": {**CARTA, "otorgante_rfc": ""}})
        assert r.status_code == 200, r.text
        doc = r.json()
        f = client.get(doc["url"])
        assert f.status_code == 200 and f.headers["content-type"] == "application/pdf"
        assert "attachment" in f.headers["content-disposition"]
        assert "María López Ruiz" in pymupdf.open(stream=f.content, filetype="pdf")[0].get_text()


def test_generate_validation_and_unknown_type(connector):
    with TestClient(app) as client:
        client.app.state.connector = connector
        bad = client.post("/api/documents", json={"type": "carta_poder", "data": {"lugar": "CDMX"}})
        assert bad.status_code == 422 and any("otorgante_nombre" in e for e in bad.json()["errors"])
        assert client.post("/api/documents", json={"type": "nope", "data": {}}).status_code == 404
        assert client.get("/api/documents/../../etc/passwd").status_code == 404


def test_config_lists_documents_and_suggestions():
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert [d["id"] for d in cfg["documents"]] == ["carta_poder", "escrito_libre_sat", "solicitud_acceso_informacion"]
        assert cfg["suggestions"]
