"""Flujo completo del dictamen técnico con las piezas reales, de la subida a la descarga.

Reales: la API (app.main), el middleware de límites, ``OficioExtractor`` (detección del
formato, lectura del PDF/Word, preparación de la foto y armado de la petición), ``schema``,
``tecnicos`` con el catálogo del repo, ``generate`` con la plantilla convertida y
``DocumentConnector.store_file``. Solo el LLM es un doble: un ``httpx.MockTransport`` que
responde como Qwen en ``/v1/chat/completions``, para que la suite no dependa de la red.
"""

from __future__ import annotations

import io
import json
import random
import re
import zipfile
from pathlib import Path

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient
from lxml import etree

from app.dictamen import generate, schema
from app.dictamen.extract import DOCX_MIME, PROMPT, OficioExtractor
from app.limits import TOO_LARGE, RequestLimits
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dictamen"
GROUND_TRUTH = json.loads((FIXTURES / "oficio_ground_truth.json").read_text(encoding="utf-8"))

# Lo que completa la persona en la tarjeta (sección «Datos del dictamen»).
OWN = {
    "numero_oficio": "FGEO/CGSI/1429/2026",
    "dictamen_inicial": "191",
    "iniciales": "igf, dsa",
    "motivo": "EQUIPO CON FALLA DE ENCENDIDO.",
}


def qwen_reply(gt: dict) -> dict:
    """Respuesta típica de Qwen para un oficio: fecha en texto con «de», mayúsculas mezcladas y
    celdas vacías como null (la normalización las deja como en el ground truth)."""
    reply = json.loads(json.dumps(gt))
    day, _, month, _, year = gt["fecha_oficio_recibido"].split()
    reply["fecha_oficio_recibido"] = f"{int(day)} de {month.lower()} de {year}"
    reply["cargo_remitente"] = gt["cargo_remitente"].title()
    return reply


class FakeQwen:
    """Servidor OpenAI-compatible mínimo; guarda cada petición para revisarla."""

    def __init__(self, reply):
        self.reply = reply
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        self.bodies.append(json.loads(request.content))
        if isinstance(self.reply, Exception):
            raise self.reply
        if isinstance(self.reply, httpx.Response):
            return self.reply
        content = self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
        })


def _limits_of(asgi_app) -> RequestLimits | None:
    node = asgi_app.middleware_stack
    while node is not None and not isinstance(node, RequestLimits):
        node = getattr(node, "app", None)
    return node


@pytest.fixture
def client(connector):
    with TestClient(app) as c:
        limits = _limits_of(c.app)
        if limits is not None:
            limits.recent.clear()  # la suite comparte el presupuesto de POST por minuto
        c.app.state.connector = connector  # los DOCX van a tmp_path, no a data/generated
        c.app.state.dictamen_enabled = True  # aunque el entorno lo apague
        yield c


def use_qwen(c: TestClient, reply) -> FakeQwen:
    fake = FakeQwen(reply)
    s = c.app.state.settings
    c.app.state.dictamen_extractor = OficioExtractor(
        s.llm_base_url, s.dictamen_model, api_key=s.llm_api_key, timeout_s=10,
        max_pages=s.dictamen_max_pages, transport=httpx.MockTransport(fake),
    )
    return fake


def upload(c: TestClient, name: str, content: bytes | None = None):
    content = (FIXTURES / name).read_bytes() if content is None else content
    # el mime del navegador no se usa: el servidor detecta el formato por los bytes
    return c.post("/api/dictamen/oficio", files={"file": (name, content, "application/octet-stream")},
                  data={"conversation_id": "conv-1"})


def with_motivo(gt: dict) -> dict:
    return {**gt, "equipos": [{**eq, "motivo": None} for eq in gt["equipos"]]}


# ---------------------------------------------------------------- revisión del DOCX
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_review(content: bytes) -> dict:
    """Texto por párrafo (cuerpo, tablas, encabezado y pie) y XML crudo de todas las partes."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    assert zf.testzip() is None
    raw = {}
    for name in zf.namelist():
        data = zf.read(name)
        if name.endswith((".xml", ".rels")):
            etree.fromstring(data, etree.XMLParser(resolve_entities=False))  # XML válido, estricto
            raw[name] = data.decode("utf-8")
    doc = Document(io.BytesIO(content))  # abre con python-docx
    paragraphs = ["".join(t.text or "" for t in p.iter(W + "t")) for p in doc.element.body.iter(W + "p")]
    for section in doc.sections:
        for part in (section.header, section.footer):
            paragraphs += ["".join(t.text or "" for t in p.iter(W + "t")) for p in part._element.iter(W + "p")]
    return {"paragraphs": [p for p in paragraphs if p.strip()], "raw": raw, "doc": doc}


def assert_clean_package(review: dict) -> None:
    everything = "\n".join(review["raw"].values())
    for forbidden in ("MERGEFIELD", "mailMerge", "Centinela", "EFREN", "base.dictamen.xlsx", "C:\\Users", "{{", "{%"):
        assert forbidden not in everything, forbidden
    core = etree.fromstring(review["raw"]["docProps/core.xml"].encode())
    ns = {"dc": "http://purl.org/dc/elements/1.1/",
          "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"}
    for tag in ("dc:creator", "cp:lastModifiedBy"):
        assert all(not (el.text or "").strip() for el in core.findall(tag, ns)), tag
    assert not core.findall("cp:lastPrinted", ns)
    text = "\n".join(review["paragraphs"])
    assert "}}" not in text and "%}" not in text
    assert ".." not in text


def table_rows(doc) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for table in doc.tables for row in table.rows]


# ---------------------------------------------------------------- flujo completo
def test_flujo_completo_pdf_con_texto(client):
    gt = GROUND_TRUTH["A"]
    qwen = use_qwen(client, qwen_reply(gt))

    # 1) subir el oficio: PDF con capa de texto -> modo texto, sin imágenes
    r = upload(client, "oficio_texto.pdf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "oficio_texto.pdf"
    assert (body["mode"], body["pages"], body["total_pages"]) == ("texto", 1, 1)
    assert isinstance(body["elapsed_s"], float)

    (request,) = qwen.bodies
    s = client.app.state.settings
    assert request["model"] == s.dictamen_model and request["temperature"] == 0
    assert request["reasoning_effort"] == "none" and request["stream"] is False
    assert request["response_format"]["type"] == "json_schema"
    parts = request["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["text", "text"]
    assert parts[-1]["text"] == PROMPT  # las reglas por campo van en el prompt
    for value in ("FGEO/DA/0532/2026", "MXL1234ABC", "CN0KW3G1QDC009A1B2LL", "FGEO-IMP-001877"):
        assert value in parts[0]["text"]  # texto real del PDF

    # 2) la tarjeta recibe TODO lo extraído, normalizado, y los faltantes con nombres del bot
    assert body["data"] == with_motivo(gt) | {"cargo_remitente": "Directora Administrativa"}
    assert body["own"] == schema.empty_draft()["own"]
    assert body["faltantes"] == ["numero_oficio", "dictamen_inicial", "iniciales", "motivo"]

    # 3) la persona completa sus datos y genera
    r = client.post("/api/dictamen/generar", json={"data": body["data"], "own": OWN, "conversation_id": "conv-1"})
    assert r.status_code == 200, r.text
    result = r.json()
    today = generate.hoy_mexico()
    assert result["numeros"] == [191, 193] and result["iniciales"] == "igf/dsa"
    assert result["filename"] == f"dictamen_FGEO_CGSI_1429_2026_{today.isoformat()}.docx"
    assert result["mime_type"] == DOCX_MIME and result["title"] == "Dictamen técnico"
    assert result["url"] == f"/api/documents/{result['id']}"

    # 4) descargar y revisar el DOCX
    f = client.get(result["url"])
    assert f.status_code == 200 and f.headers["content-type"] == DOCX_MIME
    assert f.headers["cache-control"] == "private, no-store" and len(f.content) == result["size"]
    review = docx_review(f.content)
    assert_clean_package(review)
    paragraphs = review["paragraphs"]
    text = "\n".join(paragraphs)

    fecha = generate.fecha_larga(today).upper()
    assert f"Oax., a {fecha}" in text
    for value in ("FGEO/CGSI/1429/2026", "MTRA. LAURA ISABEL MENDOZA CRUZ", "DIRECTORA ADMINISTRATIVA",
                  "su oficio FGEO/DA/0532/2026 de fecha 20 DE AGOSTO DEL 2026", "de los siguientes equipos"):
        assert value in text, value
    assert "Directora Administrativa" not in text  # todo en mayúsculas

    rows = table_rows(review["doc"])
    assert ["191", "FGEO-CPU-004512", "COMPUTADORA DE ESCRITORIO", "HP", "PRODESK 400 G7", "MXL1234ABC"] in rows
    assert ["192", "FGEO-IMP-001877", "IMPRESORA MULTIFUNCIONAL", "EPSON", "L3250", "X8HK012345"] in rows
    assert ["193", "S/N", "MONITOR", "DELL", "P2219H", "CN0KW3G1QDC009A1B2LL"] in rows

    # un dictamen por equipo, cada uno con su número, la fecha y el motivo global sin «..»
    numeros = [paragraphs[i + 1] for i, p in enumerate(paragraphs) if p == "No. DICTAMEN"]
    assert numeros == ["191", "192", "193"]
    assert text.count(f"\n{fecha}\n") == 3
    motivo = "se emite el siguiente dictamen: EQUIPO CON FALLA DE ENCENDIDO."
    assert sum(motivo in p for p in paragraphs) == 3
    # «MDTG/» de la plantilla + las iniciales, sin MDTG duplicado: en el oficio y en cada dictamen
    assert [p for p in paragraphs if "MDTG" in p] == ["MDTG/igf/dsa"] * 4
    assert "S/N" in [row[1] for row in rows]


@pytest.mark.parametrize(
    ("name", "mode", "gt_key"),
    [("oficio_foto.jpg", "imagen", "A"), ("oficio.png", "imagen", "A"), ("oficio_escaneado.pdf", "imagen", "A"),
     ("oficio_tabla.docx", "texto", "B")],
)
def test_otros_formatos_llegan_al_modelo_como_corresponde(client, name, mode, gt_key):
    gt = GROUND_TRUTH[gt_key]
    qwen = use_qwen(client, qwen_reply(gt))
    r = upload(client, name)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == mode and body["pages"] == 1
    parts = qwen.bodies[0]["messages"][0]["content"]
    if mode == "imagen":
        images = [p for p in parts if p["type"] == "image_url"]
        assert len(images) == 1 and re.match(r"data:image/(png|jpeg);base64,", images[0]["image_url"]["url"])
    else:
        assert gt["oficio_recibido"] in parts[0]["text"] and gt["equipos"][0]["numero_serie"] in parts[0]["text"]
    assert body["data"]["equipos"] == with_motivo(gt)["equipos"]
    assert body["data"]["fecha_oficio_recibido"] == gt["fecha_oficio_recibido"]


def test_subida_mayor_que_el_tope_general_pasa_por_su_propia_ruta(client):
    """Una foto de >64 KiB (el tope de los demás POST) llega al extractor; /api/chat sigue en 413."""
    from PIL import Image

    rnd = random.Random(7)
    img = Image.frombytes("RGB", (320, 320), bytes(rnd.getrandbits(8) for _ in range(320 * 320 * 3)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert len(buf.getvalue()) > client.app.state.settings.max_request_bytes
    use_qwen(client, {"equipos": []})
    r = upload(client, "foto.png", buf.getvalue())
    assert r.status_code == 200 and r.json()["mode"] == "imagen"
    assert r.json()["faltantes"][:5] == ["oficio_recibido", "fecha_oficio_recibido", "cargo_remitente",
                                         "nombre_remitente", "equipos"]
    big = {"messages": [{"role": "user", "content": "x" * 70_000}]}
    r = client.post("/api/chat", json=big)
    assert r.status_code == 413 and r.json()["message"] == TOO_LARGE


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (httpx.Response(500, text="error interno de ollama"), "HTTP 500"),
        ("esto no es JSON", "legibles"),
        (httpx.ConnectError("sin red"), "conectarme"),
    ],
)
def test_fallo_del_modelo_devuelve_tarjeta_vacia_para_captura_manual(client, reply, fragment):
    use_qwen(client, reply)
    r = upload(client, "oficio_texto.pdf")
    assert r.status_code == 502
    body = r.json()
    assert fragment in body["message"] and body["filename"] == "oficio_texto.pdf"
    assert body["data"] == schema.empty_draft()["data"] and body["own"] == schema.empty_draft()["own"]
    # sin equipos el motivo general todavía no se pide (depende de los motivos propios)
    assert body["faltantes"] == ["oficio_recibido", "fecha_oficio_recibido", "cargo_remitente", "nombre_remitente",
                                 "equipos", "numero_oficio", "dictamen_inicial", "iniciales"]
    assert "ollama" not in body["message"]


def test_generar_con_plantilla_real_corrige_los_bugs_del_original(client):
    """Un equipo, motivo propio con punto final, MDTG escrito por la persona y claves ajenas."""
    data = {
        "oficio_recibido": "fgeo/vrc/1187/2026",
        "fecha_oficio_recibido": "2026-09-03",
        "cargo_remitente": "Vicefiscal Regional de la Costa",
        "nombre_remitente": "Lic. Sofía Guadalupe Hernández Vásquez",
        "fecha_larga": "01 DE ENERO DEL 1999",  # no está en la lista blanca
        "equipos": [{"tipo_equipo": "laptop", "marca": "lenovo", "modelo": "ThinkPad E14", "numero_serie": "PF2ABC12",
                     "numero_inventario": "", "motivo": "Bisagra rota.", "num_dictamen": "666"}],
    }
    own = {"numero_oficio": "FGEO/CGSI/1430/2026", "dictamen_inicial": "0007", "iniciales": "MDTG/IGF", "motivo": ""}
    r = client.post("/api/dictamen/generar", json={"data": data, "own": own})
    assert r.status_code == 200, r.text
    assert r.json()["numeros"] == ["0007", "0007"] and r.json()["iniciales"] == "igf"
    review = docx_review(client.get(r.json()["url"]).content)
    assert_clean_package(review)
    text = "\n".join(review["paragraphs"])
    assert "del siguiente equipo" in text and "03 DE SEPTIEMBRE DEL 2026" in text
    assert "LIC. SOFÍA GUADALUPE HERNÁNDEZ VÁSQUEZ" in text and "FGEO/VRC/1187/2026" in text
    assert "se emite el siguiente dictamen: BISAGRA ROTA." in text
    assert [p for p in review["paragraphs"] if "MDTG" in p] == ["MDTG/igf"] * 2
    assert "1999" not in text and "666" not in text
    assert ["0007", "S/N", "LAPTOP", "LENOVO", "THINKPAD E14", "PF2ABC12"] in table_rows(review["doc"])


def test_generar_rechaza_lo_que_el_original_aceptaba(client):
    data = with_motivo(GROUND_TRUTH["A"])
    cases = [
        ({**data, "equipos": []}, OWN, ["equipos"]),  # 0 equipos ya no da un dictamen vacío
        (data, {**OWN, "dictamen_inicial": "19a"}, ["dictamen_inicial"]),  # antes imprimía NaN
        (data, {**OWN, "motivo": None}, ["motivo"]),  # ningún equipo trae motivo propio
    ]
    for d, o, faltantes in cases:
        r = client.post("/api/dictamen/generar", json={"data": d, "own": o})
        assert r.status_code == 422 and r.json()["faltantes"] == faltantes, r.text
    r = client.post("/api/dictamen/generar", json={"data": data, "own": {**OWN, "iniciales": "MDTG"}})
    assert r.status_code == 422 and r.json()["field"] == "iniciales" and "MDTG" in r.json()["message"]
    assert client.app.state.connector.storage_dir.exists() is False or not any(
        client.app.state.connector.storage_dir.glob("*.bin"))
