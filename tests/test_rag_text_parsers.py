from app.rag.parsers import html_to_text, parse_law, parse_sat_fichas
from app.rag.text import PAGE_BREAK, clean_pages, split_text, strip_page_breaks

HEADER = ["LEY DE PRUEBA", "CÁMARA DE DIPUTADOS DEL H. CONGRESO DE LA UNIÓN", "Última Reforma DOF 01-01-2026"]


def _pages(bodies):
    return ["\n".join(HEADER + body.split("\n") + [f"{i + 1} de {len(bodies)}"]) for i, body in enumerate(bodies)]


def test_clean_pages_removes_headers_and_keeps_page_breaks():
    text = clean_pages(_pages(["Artículo 1o.- Uno.", "Artículo 2o.- Dos.", "Artículo 3o.- Tres."]))
    assert "CÁMARA DE DIPUTADOS" not in text
    assert "1 de 3" not in text
    assert text.count(PAGE_BREAK) == 2


def test_normalize_joins_wrapped_lines_and_enumerations():
    text = clean_pages(_pages(["Las personas físicas que\nrealicen actividades:\nI.\nPresentar avisos.\na)\nDeban declarar, o\nb)\nEstén obligadas.", "x", "y"]))
    assert "Las personas físicas que realicen actividades:" in text
    assert "I. Presentar avisos." in text
    assert "a) Deban declarar, o\nb) Estén obligadas." in text


def test_reform_notes_are_dropped():
    text = clean_pages(_pages(["Artículo 5.- Texto.\nPárrafo reformado DOF 12-11-2021", "a", "b"]))
    assert "reformado DOF" not in text


def test_split_text_respects_size_and_tracks_pages():
    body = "\n".join([f"Párrafo {i} " + "x" * 300 for i in range(4)] + [PAGE_BREAK] + [f"Página dos {i} " + "y" * 300 for i in range(4)])
    chunks = split_text(body, start_page=10, max_chars=700, overlap=0)
    assert all(len(c.text) <= 700 for c in chunks)
    assert chunks[0].page == 10
    assert chunks[-1].page == 11
    assert all(PAGE_BREAK not in strip_page_breaks(c.text) for c in chunks)


LAW = "\n".join(
    [
        "DECRETO por el que se expide la Ley de Prueba",
        "TÍTULO PRIMERO",
        "Disposiciones Generales",
        "Artículo 1o.- Esta ley es de orden público.",
        "Artículo 4o.-A.- Artículo con letra.",
        PAGE_BREAK,
        "CAPÍTULO IV",
        "Vacaciones",
        "Artículo 76.- Las personas trabajadoras disfrutarán de vacaciones.",
        "Artículo 17-H Bis.- Supuesto bis.",
        "ARTÍCULO 86-E.- Mayúsculas y letra.",
        "TRANSITORIOS",
        "Artículo 1o.- Entra en vigor al día siguiente.",
    ]
)


def test_parse_law_splits_articles_with_context_and_cuts_transitorios():
    sections = parse_law(LAW, "LP")
    names = [s.section for s in sections]
    assert names == ["Artículo 1o", "Artículo 4o-A", "Artículo 76", "Artículo 17-H Bis", "Artículo 86-E"]
    art76 = sections[2]
    assert "CAPÍTULO IV (Vacaciones)" in art76.heading
    assert "TÍTULO PRIMERO (Disposiciones Generales)" in art76.heading
    assert art76.page == 2
    # el encabezado del capítulo no se queda pegado al artículo anterior
    assert "CAPÍTULO IV" not in sections[1].text and "Vacaciones" not in sections[1].text
    assert "Entra en vigor" not in sections[-1].text


FICHAS = "\n".join(
    [
        "Contenido",
        "1/CFF",
        "Solicitud de inscripción en el RFC de personas físicas.",
        "2/CFF",
        "Aviso de prueba.",
        "I. Definiciones",
        "1.1. CURP, la Clave Única de Registro de Población a 18 posiciones, usada en muchos trámites del SAT.",
        "Identificaciones oficiales aceptadas: credencial para votar, pasaporte vigente y cédula profesional.",
        "1/CFF Solicitud de inscripción en el RFC de personas físicas.",
        "Trámite",
        "Descripción del trámite o servicio",
        "Solicita la inscripción en el RFC.",
        PAGE_BREAK,
        "2/CFF Aviso de prueba.",
        "Trámite",
        "Descripción del trámite o servicio",
        "Presenta el aviso.",
    ]
)


def test_parse_sat_fichas():
    sections = parse_sat_fichas(FICHAS, "Anexo 2")
    assert [s.section for s in sections] == ["Disposiciones generales", "Ficha 1/CFF", "Ficha 2/CFF"]
    assert "Contenido" not in sections[0].text and "CURP" in sections[0].text
    assert sections[1].heading == "Anexo 2, ficha 1/CFF: Solicitud de inscripción en el RFC de personas físicas."
    assert sections[2].page == 2


def test_html_to_text_extracts_main():
    title, text = html_to_text(
        "<html><head><title>Trámite X</title><script>var a</script></head><body><nav>menú</nav>"
        "<main><h1>Requisitos</h1><p>Identificación oficial vigente.</p></main><footer>pie</footer></body></html>"
    )
    assert title == "Trámite X"
    assert "Identificación oficial vigente." in text and "menú" not in text and "pie" not in text
