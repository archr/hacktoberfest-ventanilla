"""Limpieza de texto extraído de PDFs/HTML y partición en fragmentos.

Las páginas se unen con una línea que contiene solo PAGE_BREAK para poder
calcular a qué página del PDF pertenece cada fragmento (y enlazar con #page=N).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

PAGE_BREAK = "\f"

_PAGE_NUMBER = re.compile(r"^(?:p[áa]gina\s+)?\d+\s*(?:de|/)\s*\d+$", re.I)
_REFORM_NOTE = re.compile(
    r"^[A-ZÁÉÍÓÚ][\w\s,\.\-]{0,80}?(?:reformad|adicionad|derogad|recorrid)[oa]s?\b.{0,120}?"
    r"DOF\s+\d{2}-\d{2}-\d{4}[\s\.,;A-Za-zÁÉÍÓÚáéíóú\d\-]*$"
)


def clean_pages(pages: list[str], head: int = 6, tail: int = 4, min_ratio: float = 0.4) -> str:
    """Quita encabezados/pies repetidos y une las páginas con PAGE_BREAK."""
    split = [[ln.strip() for ln in p.splitlines()] for p in pages]
    counts: Counter[str] = Counter()
    for lines in split:
        nonempty = [ln for ln in lines if ln]
        counts.update(set(nonempty[:head] + nonempty[-tail:]))
    threshold = max(3, int(len(pages) * min_ratio))
    boilerplate = {ln for ln, c in counts.items() if c >= threshold}

    out: list[str] = []
    for i, lines in enumerate(split):
        nonempty_idx = [j for j, ln in enumerate(lines) if ln]
        edge = set(nonempty_idx[:head] + nonempty_idx[-tail:])
        kept = [
            ln
            for j, ln in enumerate(lines)
            if not (j in edge and (ln in boilerplate or _PAGE_NUMBER.match(ln)))
        ]
        if i:
            out.append(PAGE_BREAK)
        out.extend(kept)
    return normalize_lines(out)


def normalize_lines(lines: list[str]) -> str:
    """Une líneas cortadas por el ancho de página y elimina notas de reforma."""
    merged: list[str] = []
    for raw in lines:
        if raw == PAGE_BREAK:  # antes de strip(): "\f" cuenta como espacio en blanco
            merged.append(PAGE_BREAK)
            continue
        ln = re.sub(r"[ \t\u00a0]+", " ", raw).strip()
        if not ln or ln in {"\uf0b7"}:
            continue
        if _REFORM_NOTE.match(ln) and len(ln) < 220:
            continue
        prev_idx = _last_text_index(merged)
        if prev_idx is not None:
            prev = merged[prev_idx]
            if prev.endswith("-") and ln[:1].islower() and not prev.endswith(" -"):
                merged[prev_idx] = prev[:-1] + ln
                continue
            if _continues(prev, ln):
                merged[prev_idx] = prev + " " + ln
                continue
        merged.append(ln)
    return "\n".join(merged)


def _last_text_index(lines: list[str]) -> int | None:
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] != PAGE_BREAK:
            return i
    return None


_ENUM = re.compile(r"^(?:[IVXLC]+\.|[a-z]\)|\d+\.|•|-)\s*$")
_BLOCK_START = re.compile(r"^(?:Art[íi]culo\s+\d|\d+/[A-Z]|T[ÍI]TULO\b|CAP[ÍI]TULO\b)")


def _continues(prev: str, nxt: str) -> bool:
    if _BLOCK_START.match(nxt) or _ENUM.match(nxt):
        return False
    if _ENUM.match(prev):
        return True  # "I." en una línea y el texto en la siguiente
    if prev.endswith((".", ":", ";", "?", "!")):
        return False
    first = nxt[:1]
    return first.islower() or first in "(,\"“«" or prev.endswith(",")


def strip_page_breaks(text: str) -> str:
    return "\n".join(ln for ln in text.split("\n") if ln != PAGE_BREAK).strip()


def page_at(text: str, offset: int, start_page: int = 1) -> int:
    return start_page + text.count(PAGE_BREAK, 0, offset)


@dataclass
class TextChunk:
    text: str
    page: int | None


def split_text(
    text: str, start_page: int | None = 1, max_chars: int = 1800, overlap: int = 250
) -> list[TextChunk]:
    """Parte el texto por párrafos en fragmentos de ~max_chars con solapamiento."""
    page = start_page
    chunks: list[TextChunk] = []
    cur: list[str] = []
    cur_len = 0
    cur_page = page

    def flush() -> None:
        if cur:
            chunks.append(TextChunk("\n".join(cur), cur_page))

    for para in text.split("\n"):
        if para == PAGE_BREAK:
            if page is not None:
                page += 1
                if not cur:
                    cur_page = page
            continue
        para = para.strip()
        if not para:
            continue
        for piece in _hard_split(para, max_chars):
            if cur and cur_len + len(piece) + 1 > max_chars:
                flush()
                tail: list[str] = []
                tail_len = 0
                for prev in reversed(cur):
                    if tail_len + len(prev) > overlap:
                        break
                    tail.insert(0, prev)
                    tail_len += len(prev) + 1
                cur, cur_len, cur_page = tail, tail_len, page
            cur.append(piece)
            cur_len += len(piece) + 1
    flush()
    return chunks


def _hard_split(para: str, max_chars: int) -> list[str]:
    if len(para) <= max_chars:
        return [para]
    sentences = re.split(r"(?<=[\.;:])\s+", para)
    out: list[str] = []
    buf = ""
    for s in sentences:
        while len(s) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.append(s[:max_chars])
            s = s[max_chars:]
        if buf and len(buf) + len(s) + 1 > max_chars:
            out.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        out.append(buf)
    return out
