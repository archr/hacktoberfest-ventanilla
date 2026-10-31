"""Plantillas PDF de ejemplo (fpdf2, fuentes base latin-1)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fpdf import FPDF

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
_REPLACEMENTS = {"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", "•": "-"}


def latin(text: object) -> str:
    s = str(text or "")
    for a, b in _REPLACEMENTS.items():
        s = s.replace(a, b)
    return s.encode("latin-1", errors="replace").decode("latin-1")


def fecha_larga(value: str | None) -> str:
    d: date
    try:
        d = date.fromisoformat(value) if value else datetime.now(ZoneInfo("America/Mexico_City")).date()
    except ValueError:
        return value or ""
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


class _Pdf(FPDF):
    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7.5)
        self.set_text_color(130, 130, 130)
        self.cell(
            0, 8,
            latin("Documento de ejemplo generado por el servicio simulado del MVP. Revísalo antes de usarlo."),
            align="C",
        )


def _new() -> _Pdf:
    pdf = _Pdf(format="Letter")
    pdf.set_margins(25, 22, 25)
    pdf.set_auto_page_break(True, margin=22)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    return pdf


def _p(pdf: FPDF, text: str, bold: bool = False, align: str = "J", size: float = 11, gap: float = 3) -> None:
    pdf.set_font("Helvetica", "B" if bold else "", size)
    pdf.multi_cell(0, 6, latin(text), align=align, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(gap)


def _signature(pdf: FPDF, label: str, name: str, x: float, y: float, w: float = 70) -> None:
    pdf.set_xy(x, y)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(w, 5, "_" * 34, align="C", new_x="LEFT", new_y="NEXT")
    pdf.set_x(x)
    pdf.set_font("Helvetica", "B", 9.5)
    pdf.multi_cell(w, 5, latin(name), align="C", new_x="LEFT", new_y="NEXT")
    pdf.set_x(x)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(w, 5, latin(label), align="C")


def carta_poder(d: dict) -> bytes:
    pdf = _new()
    _p(pdf, "CARTA PODER", bold=True, align="C", size=14, gap=6)
    _p(pdf, f"{d.get('lugar', '')}, a {fecha_larga(d.get('fecha'))}.", align="R", gap=6)
    _p(pdf, str(d.get("dependencia", "")).upper(), bold=True, gap=0)
    _p(pdf, "P R E S E N T E", bold=True, gap=6)
    rfc = f", con RFC {d['otorgante_rfc']}" if d.get("otorgante_rfc") else ""
    _p(
        pdf,
        f"Por medio de la presente, yo, {d.get('otorgante_nombre', '')}{rfc}, otorgo poder amplio, "
        f"cumplido y bastante a {d.get('apoderado_nombre', '')} para que, en mi nombre y representación, "
        f"realice ante {d.get('dependencia', '')} el siguiente trámite: {d.get('tramite', '')}; pudiendo "
        "para tal efecto presentar, firmar y recibir toda clase de documentos relacionados con el mismo.",
        gap=4,
    )
    _p(pdf, "Lo anterior para los fines legales que correspondan.", gap=14)
    y = pdf.get_y() + 6
    _signature(pdf, "Otorga el poder", d.get("otorgante_nombre", ""), 25, y)
    _signature(pdf, "Acepta el poder", d.get("apoderado_nombre", ""), 120, y)
    y += 40
    _signature(pdf, "Testigo", d.get("testigo1_nombre", ""), 25, y)
    _signature(pdf, "Testigo", d.get("testigo2_nombre", ""), 120, y)
    pdf.set_xy(25, y + 32)
    _p(
        pdf,
        "Nota: anexa copia de identificación oficial de quienes firman. Algunas dependencias (por ejemplo el SAT) "
        "pueden requerir que las firmas se ratifiquen ante la autoridad o ante fedatario público.",
        size=8.5,
    )
    return bytes(pdf.output())


def escrito_libre_sat(d: dict) -> bytes:
    pdf = _new()
    _p(pdf, f"{d.get('lugar', '')}, a {fecha_larga(d.get('fecha'))}.", align="R", gap=2)
    _p(pdf, f"ASUNTO: {d.get('asunto', '')}", bold=True, align="R", gap=6)
    _p(pdf, str(d.get("autoridad", "")).upper(), bold=True, align="L", gap=0)
    _p(pdf, "SERVICIO DE ADMINISTRACIÓN TRIBUTARIA", bold=True, gap=0)
    _p(pdf, "P R E S E N T E", bold=True, gap=6)
    rep = (
        f", por conducto de mi representante legal {d['representante_legal']}"
        if d.get("representante_legal")
        else ""
    )
    _p(
        pdf,
        f"{d.get('nombre', '')}, con clave en el Registro Federal de Contribuyentes {d.get('rfc', '')}, "
        f"con domicilio fiscal en {d.get('domicilio_fiscal', '')}, y señalando como dirección de correo "
        f"electrónico para recibir notificaciones {d.get('correo_electronico', '')}{rep}, con fundamento en "
        "los artículos 8o. de la Constitución Política de los Estados Unidos Mexicanos, 18 y 18-A del Código "
        "Fiscal de la Federación, comparezco respetuosamente para exponer lo siguiente:",
        gap=5,
    )
    _p(pdf, "HECHOS", bold=True, align="C")
    _p(pdf, d.get("hechos", ""), gap=5)
    _p(pdf, "PETICIÓN", bold=True, align="C")
    _p(pdf, d.get("peticion", ""), gap=5)
    anexos = d.get("documentos_anexos") or []
    if anexos:
        _p(pdf, "ANEXOS", bold=True, align="C")
        _p(pdf, "\n".join(f"{i}. {a}" for i, a in enumerate(anexos, 1)), align="L", gap=5)
    _p(
        pdf,
        "Por lo anteriormente expuesto, atentamente solicito se tenga por presentado este escrito y se "
        "acuerde de conformidad lo solicitado.",
        gap=10,
    )
    _p(pdf, "PROTESTO LO NECESARIO", bold=True, align="C", gap=18)
    firmante = d.get("representante_legal") or d.get("nombre", "")
    _signature(pdf, "Firma", firmante, 70, pdf.get_y())
    return bytes(pdf.output())


def solicitud_acceso_informacion(d: dict) -> bytes:
    pdf = _new()
    _p(pdf, "SOLICITUD DE ACCESO A LA INFORMACIÓN PÚBLICA", bold=True, align="C", size=13, gap=6)
    lugar = f"{d['lugar']}, a " if d.get("lugar") else ""
    _p(pdf, f"{lugar}{fecha_larga(d.get('fecha'))}.", align="R", gap=6)
    rows = [
        ("Sujeto obligado", d.get("sujeto_obligado", "")),
        ("Solicitante", d.get("solicitante_nombre") or "Anónimo / seudónimo"),
        ("Medio para recibir notificaciones", d.get("medio_notificacion", "")),
        ("Modalidad de entrega", d.get("modalidad_entrega", "")),
    ]
    for label, value in rows:
        pdf.set_font("Helvetica", "B", 10.5)
        pdf.multi_cell(0, 6, latin(f"{label}:"), new_x="LMARGIN", new_y="NEXT")
        _p(pdf, value, align="L", gap=2)
    pdf.ln(2)
    _p(pdf, "Descripción de la información solicitada:", bold=True, gap=1)
    _p(pdf, d.get("descripcion_informacion", ""), gap=4)
    if d.get("datos_adicionales"):
        _p(pdf, "Datos adicionales para facilitar la búsqueda:", bold=True, gap=1)
        _p(pdf, d["datos_adicionales"], gap=4)
    _p(
        pdf,
        "Con fundamento en el artículo 6o. de la Constitución Política de los Estados Unidos Mexicanos y en la "
        "legislación de transparencia y acceso a la información pública aplicable, solicito se me proporcione "
        "la información descrita en la modalidad señalada.",
        gap=4,
    )
    return bytes(pdf.output())


TEMPLATES: dict[str, Callable[[dict], bytes]] = {
    "carta_poder": carta_poder,
    "escrito_libre_sat": escrito_libre_sat,
    "solicitud_acceso_informacion": solicitud_acceso_informacion,
}
