"""Genera los oficios SINTÉTICOS (datos ficticios) que usan las pruebas del dictamen.

Uso:  python tests/fixtures/dictamen/oficio_generar.py [--exp DIR]

Escribe junto a este archivo (pequeños, para pruebas unitarias):
  oficio_texto.pdf       oficio A con capa de texto (3 equipos, el monitor sin inventario)
  oficio_escaneado.pdf   oficio A como imagen dentro de un PDF (sin capa de texto)
  oficio.png / oficio_foto.jpg   oficio A en imagen de baja resolución
  oficio_tabla.docx      oficio B en Word: encabezado, tabla con celda combinada
                         verticalmente, celdas vacías y "S/N", firma con salto de línea
  oficio_ground_truth.json   valores esperados de A y B

Con --exp DIR escribe además las entradas del experimento con Qwen (tamaño real):
  a_150dpi.png, b_texto.pdf (oficio B en PDF), b_foto_degradada.jpg (perspectiva, giro,
  desenfoque, iluminación irregular, ruido y JPEG q=55) y c_escaneado_2p.pdf (oficio C de 2
  páginas escaneado a 200 dpi, sin capa de texto; la tabla y la firma siguen en la página 2).
"""

from __future__ import annotations

import argparse
import io
import json
import random
from pathlib import Path

import pymupdf
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent

A = {
    "oficio_recibido": "FGEO/DA/0532/2026",
    "fecha_oficio_recibido": "20 DE AGOSTO DEL 2026",
    "cargo_remitente": "DIRECTORA ADMINISTRATIVA",
    "nombre_remitente": "MTRA. LAURA ISABEL MENDOZA CRUZ",
    "equipos": [
        {"tipo_equipo": "COMPUTADORA DE ESCRITORIO", "marca": "HP", "modelo": "PRODESK 400 G7",
         "numero_serie": "MXL1234ABC", "numero_inventario": "FGEO-CPU-004512"},
        {"tipo_equipo": "IMPRESORA MULTIFUNCIONAL", "marca": "EPSON", "modelo": "L3250",
         "numero_serie": "X8HK012345", "numero_inventario": "FGEO-IMP-001877"},
        {"tipo_equipo": "MONITOR", "marca": "DELL", "modelo": "P2219H",
         "numero_serie": "CN0KW3G1QDC009A1B2LL", "numero_inventario": None},
    ],
}

B = {
    "oficio_recibido": "FGEO/VRC/1187/2026",
    "fecha_oficio_recibido": "03 DE SEPTIEMBRE DEL 2026",
    "cargo_remitente": "VICEFISCAL REGIONAL DE LA COSTA",
    "nombre_remitente": "LIC. SOFÍA GUADALUPE HERNÁNDEZ VÁSQUEZ",
    "equipos": [
        {"tipo_equipo": "LAPTOP", "marca": "LENOVO", "modelo": "THINKPAD E14 GEN 2",
         "numero_serie": "PF2ABC12", "numero_inventario": "FGEO-LAP-000781"},
        {"tipo_equipo": "NO BREAK", "marca": "CDP", "modelo": "R-UPR1008",
         "numero_serie": "2104A0335117", "numero_inventario": None},
        {"tipo_equipo": "MONITOR", "marca": "HP", "modelo": "V19",
         "numero_serie": "1CR0420XYZ", "numero_inventario": "FGEO-MON-002210"},
        {"tipo_equipo": "TECLADO", "marca": "HP", "modelo": "KU-1156",
         "numero_serie": "BDMHE0B7Y8K9QZ", "numero_inventario": None},
    ],
}
C = {
    "oficio_recibido": "FGEO/DRM/2045/2026",
    "fecha_oficio_recibido": "14 DE OCTUBRE DEL 2026",
    "cargo_remitente": "DIRECTOR DE RECURSOS MATERIALES",
    "nombre_remitente": "ING. ROBERTO CARLOS JIMÉNEZ ORTIZ",
    "equipos": [
        {"tipo_equipo": "COMPUTADORA PORTÁTIL", "marca": "DELL", "modelo": "LATITUDE 3420",
         "numero_serie": "7HQ2KL3", "numero_inventario": "FGEO-LAP-001102"},
        {"tipo_equipo": "IMPRESORA LÁSER", "marca": "BROTHER", "modelo": "HL-L2350DW",
         "numero_serie": "U64021H0N123456", "numero_inventario": "FGEO-IMP-000398"},
        {"tipo_equipo": "SWITCH", "marca": "TP-LINK", "modelo": "TL-SG1016D",
         "numero_serie": "2219310008841", "numero_inventario": None},
        {"tipo_equipo": "ESCÁNER", "marca": "EPSON", "modelo": "DS-530 II",
         "numero_serie": "X5RG004417", "numero_inventario": "FGEO-ESC-000057"},
        {"tipo_equipo": "COMPUTADORA DE ESCRITORIO", "marca": "LENOVO", "modelo": "THINKCENTRE M70S",
         "numero_serie": "MJ0GQ8B1", "numero_inventario": "FGEO-CPU-006630"},
    ],
}
B_FECHA_IMPRESA = "San Pedro Pochutla, Oax., a 3 de septiembre de 2026"
B_INVENTARIO_IMPRESO = ["FGEO-LAP-000781", "", "FGEO-MON-002210", "S/N"]
B_DESTINATARIO = ["MTRO. CARLOS EDUARDO RAMÍREZ LUNA",
                  "COORDINADOR GENERAL DE SISTEMAS E INFORMÁTICA", "P R E S E N T E"]
B_CUERPO = (
    "En seguimiento al oficio FGEO/CGSI/0991/2026 y al expediente administrativo "
    "EXP-VRC/233/2026, solicito a usted la revisión y el dictamen técnico de los siguientes "
    "bienes informáticos asignados a esta Vicefiscalía, que dejaron de funcionar:"
)
B_CCP = ["C.c.p. LIC. JORGE ALBERTO RUIZ SOSA. Fiscal General. Para su conocimiento.",
         "C.c.p. C.P. MARTHA ELENA LÓPEZ DÍAZ. Control Patrimonial. Mismo fin.",
         "SGHV/jmr", "DOCUMENTO SINTÉTICO GENERADO PARA PRUEBAS - DATOS FICTICIOS"]


def oficio_a_pdf() -> bytes:
    """Mismo diseño que el oficio del análisis previo (make_oficio.py)."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_margins(20, 15, 20)
    pdf.set_fill_color(120, 30, 50)
    pdf.rect(20, 12, 18, 18, style="F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_xy(20, 18)
    pdf.cell(18, 6, "FGEO", align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.set_xy(42, 12)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 6, "FISCALÍA GENERAL DEL ESTADO DE OAXACA", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(42)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, "Dirección Administrativa", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(42)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, "\"2026, Año de la Transparencia y la Rendición de Cuentas\" (leyenda ficticia)",
             new_x="LMARGIN", new_y="NEXT")
    pdf.line(20, 33, 196, 33)
    pdf.set_y(37)
    pdf.set_font("Helvetica", "B", 10)
    for line in ["OFICIO NÚM.: FGEO/DA/0532/2026", "ASUNTO: Solicitud de dictamen técnico",
                 "Oaxaca de Juárez, Oax., a 20 DE AGOSTO DEL 2026"]:
        pdf.cell(0, 5, line, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    for line in ["ING. RAÚL ANTONIO GÓMEZ PÉREZ", "COORDINADOR GENERAL DE SISTEMAS E INFORMÁTICA",
                 "DE LA FISCALÍA GENERAL DEL ESTADO DE OAXACA", "P R E S E N T E."]:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, (
        "Por medio del presente, y con fundamento en las disposiciones aplicables en materia de control "
        "patrimonial, me permito solicitar a usted de la manera más atenta, gire sus apreciables instrucciones "
        "a quien corresponda a efecto de que se realice la revisión y se emita el dictamen técnico de los "
        "siguientes bienes informáticos asignados a esta Dirección, los cuales presentan fallas que impiden "
        "su uso:"
    ), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    cols = [("No.", 8), ("TIPO DE EQUIPO", 49), ("MARCA", 16), ("MODELO", 28), ("No. DE SERIE", 41),
            ("No. INVENTARIO", 34)]
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(225, 225, 225)
    for name, w in cols:
        pdf.cell(w, 7, name, border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for i, eq in enumerate(A["equipos"], 1):
        vals = [str(i), eq["tipo_equipo"], eq["marca"], eq["modelo"], eq["numero_serie"],
                eq["numero_inventario"] or ""]
        for (_, w), v in zip(cols, vals):
            pdf.cell(w, 7, v, border=1, align="C")
        pdf.ln()
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, (
        "Cabe señalar que el monitor descrito no cuenta con número de inventario asignado. Sin otro particular, "
        "aprovecho la ocasión para enviarle un cordial saludo."
    ), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Helvetica", "B", 10)
    for line in ["A T E N T A M E N T E", "", "", "MTRA. LAURA ISABEL MENDOZA CRUZ", "DIRECTORA ADMINISTRATIVA"]:
        pdf.cell(0, 5, line, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 7)
    for line in ["C.c.p.- LIC. JORGE ALBERTO RUIZ SOSA.- Fiscal General del Estado de Oaxaca.- Para su superior conocimiento.",
                 "C.c.p.- C.P. MARTHA ELENA LÓPEZ DÍAZ.- Jefa del Departamento de Control Patrimonial.- Mismo fin.",
                 "LIMC/rgp*", "DOCUMENTO SINTÉTICO GENERADO PARA PRUEBAS - DATOS FICTICIOS"]:
        pdf.cell(0, 4, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def oficio_b_pdf() -> bytes:
    """Oficio B en PDF (mismo contenido que el DOCX) para fotografiarlo."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_margins(22, 15, 22)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_y(14)
    pdf.cell(0, 6, "FISCALÍA GENERAL DEL ESTADO DE OAXACA", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, "Vicefiscalía Regional de la Costa", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.line(22, 27, 194, 27)
    pdf.set_y(32)
    pdf.set_font("Helvetica", "B", 10)
    for line in ["OFICIO No. FGEO/VRC/1187/2026", "ASUNTO: Se solicita dictamen técnico"]:
        pdf.cell(0, 5, line, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, B_FECHA_IMPRESA, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    for line in B_DESTINATARIO:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, B_CUERPO, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    cols = [("No.", 9), ("DESCRIPCIÓN", 30), ("MARCA", 20), ("MODELO", 38), ("SERIE", 40), ("INVENTARIO", 35)]
    pdf.set_font("Helvetica", "B", 8)
    for name, w in cols:
        pdf.cell(w, 7, name, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    y_marca = None
    for i, eq in enumerate(B["equipos"], 1):
        vals = [str(i), eq["tipo_equipo"], eq["marca"], eq["modelo"], eq["numero_serie"],
                B_INVENTARIO_IMPRESO[i - 1]]
        x0, y0 = pdf.get_x(), pdf.get_y()
        for j, ((_, w), v) in enumerate(zip(cols, vals)):
            if j == 2 and i >= 3:  # MARCA combinada en las filas 3 y 4
                if i == 3:
                    y_marca = y0
                    pdf.cell(w, 7, "", border="LTR")
                else:
                    pdf.cell(w, 7, "", border="LBR")
                    x_marca = x0 + cols[0][1] + cols[1][1]
                    pdf.set_xy(x_marca, y_marca)
                    pdf.cell(w, 14, "HP", align="C")
                    pdf.set_xy(x_marca + w, y0)
                continue
            pdf.cell(w, 7, v, border=1, align="C")
        pdf.ln()
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, "Sin otro particular, reciba un cordial saludo.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 10)
    for line in ["A T E N T A M E N T E", "", "", "LIC. SOFÍA GUADALUPE HERNÁNDEZ VÁSQUEZ",
                 "VICEFISCAL REGIONAL DE LA COSTA"]:
        pdf.cell(0, 5, line, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 7)
    for line in B_CCP:
        pdf.cell(0, 4, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def oficio_c_pdf() -> bytes:
    """Oficio C en 2 páginas: la tabla continúa en la página 2, donde también va la firma."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(22, 15, 22)
    cols = [("No.", 9), ("BIEN", 44), ("MARCA", 22), ("MODELO", 34), ("No. SERIE", 32), ("INVENTARIO", 31)]

    def header_row():
        pdf.set_font("Helvetica", "B", 8)
        for name, w in cols:
            pdf.cell(w, 7, name, border=1, align="C")
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)

    def row(i, eq):
        vals = [str(i), eq["tipo_equipo"], eq["marca"], eq["modelo"], eq["numero_serie"],
                eq["numero_inventario"] or ""]
        for (_, w), v in zip(cols, vals):
            pdf.cell(w, 7, v, border=1, align="C")
        pdf.ln()

    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 6, "FISCALÍA GENERAL DEL ESTADO DE OAXACA", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 5, "Dirección de Recursos Materiales", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 10)
    for line in ["Oficio número: FGEO/DRM/2045/2026", "Asunto: Dictamen de bienes informáticos",
                 "Reyes Mantecón, San Bartolo Coyotepec, Oax., 14 de octubre de 2026"]:
        pdf.cell(0, 5, line, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    for line in B_DESTINATARIO:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, (
        "Con relación al inventario anual 2026, le solicito el dictamen técnico de los bienes que se "
        "relacionan a continuación, a fin de determinar si procede su baja. La relación continúa en la "
        "siguiente hoja."), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    header_row()
    for i, eq in enumerate(C["equipos"][:3], 1):
        row(i, eq)
    pdf.add_page()
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 5, "Hoja 2 de 2 del oficio FGEO/DRM/2045/2026 (continuación)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    header_row()
    for i, eq in enumerate(C["equipos"][3:], 4):
        row(i, eq)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 5, "Agradezco su atención.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "B", 10)
    for line in ["ATENTAMENTE", "", "", "ING. ROBERTO CARLOS JIMÉNEZ ORTIZ", "DIRECTOR DE RECURSOS MATERIALES"]:
        pdf.cell(0, 5, line, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 7)
    for line in ["C.c.p. Archivo.", "RCJO/lmv", "DOCUMENTO SINTÉTICO GENERADO PARA PRUEBAS - DATOS FICTICIOS"]:
        pdf.cell(0, 4, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def scanned_multipage(pdf: bytes, dpi: int, angle: float = 0.6) -> bytes:
    """Cada página como imagen JPEG (escáner de oficina, ligeramente girada), sin texto."""
    src = pymupdf.open(stream=pdf, filetype="pdf")
    out = pymupdf.open()
    for page in src:
        img = Image.open(io.BytesIO(page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY).tobytes("png")))
        img = img.rotate(angle, expand=False, fillcolor=255, resample=Image.Resampling.BICUBIC)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=buf.getvalue())
    return out.tobytes(garbage=3, deflate=True)


def oficio_b_docx() -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK

    d = docx.Document()
    core = d.core_properties
    core.author, core.last_modified_by, core.title = "Pruebas", "Pruebas", "Oficio sintético"
    hdr = d.sections[0].header
    hdr.is_linked_to_previous = False
    hdr.paragraphs[0].text = "FISCALÍA GENERAL DEL ESTADO DE OAXACA"
    hdr.add_paragraph("Vicefiscalía Regional de la Costa")
    for text in ["OFICIO No. FGEO/VRC/1187/2026", "ASUNTO: Se solicita dictamen técnico", B_FECHA_IMPRESA]:
        d.add_paragraph(text).alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for text in B_DESTINATARIO:
        d.add_paragraph().add_run(text).bold = True
    d.add_paragraph(B_CUERPO)
    headers = ["No.", "DESCRIPCIÓN", "MARCA", "MODELO", "SERIE", "INVENTARIO"]
    table = d.add_table(rows=1 + len(B["equipos"]), cols=len(headers))
    table.style = "Table Grid"
    for j, h in enumerate(headers):
        table.cell(0, j).text = h
    for i, eq in enumerate(B["equipos"], 1):
        vals = [str(i), eq["tipo_equipo"], eq["marca"], eq["modelo"], eq["numero_serie"],
                B_INVENTARIO_IMPRESO[i - 1]]
        for j, v in enumerate(vals):
            if j == 2 and i == 4:
                continue  # la combina la fila 3
            table.cell(i, j).text = v
    table.cell(3, 2).merge(table.cell(4, 2))  # MARCA "HP" combinada en las filas 3 y 4
    table.cell(3, 2).text = "HP"
    d.add_paragraph("Sin otro particular, reciba un cordial saludo.")
    d.add_paragraph("A T E N T A M E N T E").alignment = WD_ALIGN_PARAGRAPH.CENTER
    firma = d.add_paragraph()
    firma.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = firma.add_run("LIC. SOFÍA GUADALUPE HERNÁNDEZ VÁSQUEZ")
    run.bold = True
    run.add_break(WD_BREAK.LINE)
    firma.add_run("VICEFISCAL REGIONAL DE LA COSTA").bold = True
    for text in B_CCP:
        d.add_paragraph(text)
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def render(pdf: bytes, dpi: int, gray: bool = False) -> Image.Image:
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def scanned_pdf(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("L").save(buf, format="JPEG", quality=50)
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=buf.getvalue())
    return doc.tobytes(garbage=3, deflate=True)


def degrade(img: Image.Image, seed: int = 7) -> Image.Image:
    """Simula una foto de celular: perspectiva, giro, desenfoque, sombra, ruido."""
    rng = random.Random(seed)
    img = img.convert("L")
    w, h = img.size
    # perspectiva: esquinas desplazadas hasta 4 %
    d = lambda s: rng.uniform(0, 0.04) * s  # noqa: E731
    quad = (d(w), d(h), d(w), h - d(h), w - d(w), h - d(h), w - d(w), d(h))
    img = img.transform((w, h), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC, fillcolor=225)
    img = img.rotate(2.5, expand=True, fillcolor=210, resample=Image.Resampling.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(0.9))
    # iluminación irregular: degradado diagonal
    shade = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(shade)
    for x in range(0, img.size[0], 4):
        draw.rectangle([x, 0, x + 4, img.size[1]], fill=int(70 * x / img.size[0]))
    img = Image.composite(Image.new("L", img.size, 0), img, shade.point(lambda v: v // 2))
    # ruido
    px = img.load()
    for _ in range(img.size[0] * img.size[1] // 25):
        x, y = rng.randrange(img.size[0]), rng.randrange(img.size[1])
        px[x, y] = max(0, min(255, px[x, y] + rng.randint(-45, 45)))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", type=Path, help="directorio para las entradas del experimento")
    args = ap.parse_args()

    a_pdf = oficio_a_pdf()
    (HERE / "oficio_texto.pdf").write_bytes(a_pdf)
    small = render(a_pdf, 60, gray=True)
    (HERE / "oficio_escaneado.pdf").write_bytes(scanned_pdf(small))
    small.save(HERE / "oficio.png", optimize=True)
    small.save(HERE / "oficio_foto.jpg", quality=55)
    (HERE / "oficio_tabla.docx").write_bytes(oficio_b_docx())
    truth = {"A": A, "B": B, "C": C}
    (HERE / "oficio_ground_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if args.exp:
        args.exp.mkdir(parents=True, exist_ok=True)
        render(a_pdf, 150).save(args.exp / "a_150dpi.png")
        b_pdf = oficio_b_pdf()
        (args.exp / "b_texto.pdf").write_bytes(b_pdf)
        degrade(render(b_pdf, 120)).save(args.exp / "b_foto_degradada.jpg", quality=55)
        render(b_pdf, 150).save(args.exp / "b_150dpi.png")
        (args.exp / "c_escaneado_2p.pdf").write_bytes(scanned_multipage(oficio_c_pdf(), 200))
    for f in sorted(HERE.glob("oficio*")) + (sorted(args.exp.iterdir()) if args.exp else []):
        print(f"{f.name}\t{f.stat().st_size} bytes")


if __name__ == "__main__":
    main()
