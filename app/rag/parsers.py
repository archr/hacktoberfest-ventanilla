"""Parsers que convierten una fuente en secciones con sentido jurídico.

- ley:        un fragmento por artículo (con título/capítulo como contexto).
- sat_fichas: una sección por ficha de trámite del Anexo de la RMF (p. ej. "1/CFF").
- html:       páginas web (gob.mx, sat.gob.mx…), por tamaño.
- generic:    cualquier texto, por tamaño.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .text import PAGE_BREAK, normalize_lines, page_at


@dataclass
class Section:
    section: str  # identificador corto: "Artículo 27", "Ficha 1/CFF"
    heading: str  # encabezado legible para citar
    text: str  # puede contener PAGE_BREAK
    page: int | None


_ARTICLE = re.compile(
    r"(?m)^(?:Art[íi]culo|ART[ÍI]CULO)\s+"
    r"(?P<num>\d+(?:o|°|º)?\.?"
    r"(?:\.?-[A-Z]\b|\s+(?:Bis|Ter|Qu[aá]ter|Quintus|Quinquies|Sexies|Septies|Octies|Nonies)\b)*)"
    r"\s*\.\s*-?\s*"
)
_TRANSITORIOS = re.compile(r"(?mi)^(?:art[íi]culos\s+)?transitorios?\s*$")
_TITLE_LINE = re.compile(
    r"^(T[ÍI]TULO|CAP[ÍI]TULO|SECCI[ÓO]N|LIBRO|T[íi]tulo|Cap[íi]tulo|Secci[óo]n|Libro)"
    r"\s+(?:[A-ZÁÉÍÓÚ0-9]\S*)(?:\s+\S+){0,3}\s*$"
)


def _norm_article(num: str) -> str:
    num = num.strip().rstrip(".")
    num = re.sub(r"\.-", "-", num)
    num = re.sub(r"\s+", " ", num)
    return num


def _context_titles(preamble_lines: list[str], state: dict[str, str]) -> None:
    """Actualiza el Título/Capítulo vigente a partir de las líneas previas a un artículo."""
    for i, ln in enumerate(preamble_lines):
        m = _TITLE_LINE.match(ln)
        if not m:
            continue
        kind = m.group(1).upper().replace("Í", "I").replace("Ó", "O")
        label = ln.strip()
        nxt = preamble_lines[i + 1].strip() if i + 1 < len(preamble_lines) else ""
        if nxt and len(nxt) < 120 and not _TITLE_LINE.match(nxt) and nxt != PAGE_BREAK:
            label = f"{label} ({nxt})"
        label = label[:160]
        if kind in {"LIBRO", "TITULO"}:
            state.update({"TITULO": label, "CAPITULO": "", "SECCION": ""})
        elif kind == "CAPITULO":
            state.update({"CAPITULO": label, "SECCION": ""})
        else:
            state["SECCION"] = label


def parse_law(text: str, short_title: str) -> list[Section]:
    articles = list(_ARTICLE.finditer(text))
    if not articles:
        return parse_generic(text, short_title)
    m_trans = _TRANSITORIOS.search(text, articles[0].start())
    end_of_body = m_trans.start() if m_trans else len(text)

    sections: list[Section] = []
    state = {"TITULO": "", "CAPITULO": "", "SECCION": ""}
    prev_start = 0
    for i, m in enumerate(articles):
        if m.start() >= end_of_body:
            break
        # Los encabezados TÍTULO/CAPÍTULO aparecen entre el artículo anterior y este.
        _context_titles(text[prev_start : m.start()].split("\n"), state)
        prev_start = m.end()
        nxt = articles[i + 1].start() if i + 1 < len(articles) else len(text)
        body_lines = text[m.start() : min(nxt, end_of_body)].rstrip().split("\n")
        # quita los encabezados (y su nombre) que pertenecen al siguiente artículo
        cut = len(body_lines)
        for j in range(len(body_lines) - 1, max(0, len(body_lines) - 8), -1):
            if _TITLE_LINE.match(body_lines[j]):
                cut = j
        body_lines = body_lines[:cut]
        while body_lines and body_lines[-1] in (PAGE_BREAK, ""):
            body_lines.pop()
        num = _norm_article(m.group("num"))
        context = " · ".join(v for v in (state["TITULO"], state["CAPITULO"]) if v)
        heading = f"{short_title}, Artículo {num}" + (f" — {context}" if context else "")
        sections.append(
            Section(
                section=f"Artículo {num}",
                heading=heading,
                text="\n".join(body_lines),
                page=page_at(text, m.start()),
            )
        )
    return sections


_FICHA = re.compile(
    r"(?m)^(?P<code>\d+/[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)?)[ \t]+(?P<title>\S[^\n]*)$"
)


def parse_sat_fichas(text: str, short_title: str, marker: str = "Descripción del trámite") -> list[Section]:
    headers = [m for m in _FICHA.finditer(text) if marker in text[m.end() : m.end() + 800]]
    if not headers:
        return parse_generic(text, short_title)
    sections: list[Section] = []

    preamble = text[: headers[0].start()]
    index_entries = list(re.finditer(r"(?m)^\d+/[A-Z][\w-]*\b.*$", preamble))
    general_start = 0
    if index_entries:
        defs = re.search(r"(?m)^I\.\s*Definiciones", preamble[index_entries[-1].end() :])
        general_start = index_entries[-1].end() + (defs.start() if defs else 0)
    general = preamble[general_start:].strip()
    if len(general) > 200:
        sections.append(
            Section(
                section="Disposiciones generales",
                heading=f"{short_title} — Definiciones y disposiciones generales",
                text=general,
                page=page_at(text, general_start),
            )
        )

    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        code = m.group("code")
        title = m.group("title").strip()
        sections.append(
            Section(
                section=f"Ficha {code}",
                heading=f"{short_title}, ficha {code}: {title}",
                text=text[m.start() : end].strip(),
                page=page_at(text, m.start()),
            )
        )
    return sections


def html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [ln for ln in root.get_text("\n").split("\n")]
    return title, normalize_lines(lines)


def parse_html(text: str, short_title: str) -> list[Section]:
    return [Section(section="Página", heading=short_title, text=text, page=None)]


def parse_generic(text: str, short_title: str) -> list[Section]:
    return [Section(section="Documento", heading=short_title, text=text, page=1)]


PARSERS = {
    "ley": parse_law,
    "sat_fichas": parse_sat_fichas,
    "html": parse_html,
    "generic": parse_generic,
}
