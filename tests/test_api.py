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


def test_presentation_is_served_next_to_the_chat():
    with TestClient(app) as client:
        r = client.get("/presentacion", follow_redirects=False)
        assert r.status_code in (301, 307, 308) and r.headers["location"].endswith("/presentacion/")
        page = client.get("/presentacion/")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        assert 'data-src="../"' in page.text  # la demo en vivo apunta al chat de esta misma app
        for asset in ("styles.css", "deck.js", "assets/qr.svg", "assets/fonts/inter-var.woff2"):
            assert client.get(f"/presentacion/{asset}").status_code == 200, asset


def test_config_lists_documents_and_suggestions():
    with TestClient(app) as client:
        cfg = client.get("/api/config").json()
        assert [d["id"] for d in cfg["documents"]] == ["carta_poder", "escrito_libre_sat", "solicitud_acceso_informacion"]
        assert cfg["suggestions"]
