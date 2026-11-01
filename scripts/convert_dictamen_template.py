#!/usr/bin/env python3
"""Convierte la plantilla del dictamen de docxtemplater (Node) a docxtpl (Python) y la limpia.

Uso (desde la raíz del repo):

    python scripts/convert_dictamen_template.py            # original -> config/dictamen/dictamen.docx
    python scripts/convert_dictamen_template.py --check    # solo verifica que la salida esté al día
    python scripts/convert_dictamen_template.py ENTRADA.docx SALIDA.docx

Qué hace:

* ``{campo}`` -> ``{{ campo }}``; dentro de un bucle, los campos del elemento -> ``{{ eq.campo }}``
  (los demás siguen siendo globales, igual que el ámbito de docxtemplater).
* Bucle cuya apertura y cierre están en la MISMA FILA de una tabla (``{#equipos}…{/equipos}`` en
  celdas distintas): se insertan una fila antes con ``{%tr for eq in equipos %}`` y otra después con
  ``{%tr endfor %}`` (docxtpl elimina esas filas al generar y repite la fila de datos).
* Bucle de PÁRRAFOS (párrafos que solo contienen ``{#equipos}`` / ``{/equipos}``, el
  ``paragraphLoop`` de docxtemplater): ``{%p for eq in equipos %}`` / ``{%p endfor %}``.
* Bucle dentro de un mismo párrafo: ``{% for … %}…{% endfor %}``.
* Solo cambia el texto de los ``w:t``; las propiedades de cada run (formato) no se tocan.
* Limpieza: quita los campos MERGEFIELD (``w:fldChar``/``w:instrText``/``w:fldSimple``) conservando
  su texto visible; quita ``w:mailMerge`` de ``word/settings.xml`` y sus relaciones
  ``mailMergeSource`` (y el .rels si queda vacío); quita los metadatos personales de
  ``docProps/core.xml`` (creator, lastModifiedBy, lastPrinted) y vacía Company/Manager de
  ``docProps/app.xml``.

Es determinista e idempotente: correrlo dos veces produce los mismos bytes y convertir su propia
salida no cambia nada (ya no quedan etiquetas ``{campo}``, campos ni metadatos que quitar).
Falla con un mensaje claro ante lo que no sabe convertir (etiquetas partidas entre runs, bucles
anidados, secciones ``{^…}``, campos MERGEFIELD anidados…), en lugar de producir una plantilla rota.
"""

from __future__ import annotations

import argparse
import copy
import io
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "config" / "dictamen" / "original" / "dictamen_docxtemplater.docx"
DEFAULT_OUTPUT = ROOT / "config" / "dictamen" / "dictamen.docx"

# Bucles conocidos: nombre -> (variable del elemento, campos que tiene cada elemento).
# Los campos del elemento son exactamente los que arma generate.js / app/dictamen/generate.py.
DEFAULT_LOOPS: dict[str, tuple[str, frozenset[str]]] = {
    "equipos": (
        "eq",
        frozenset(
            {
                "num_dictamen",
                "tipo_equipo",
                "marca",
                "modelo",
                "numero_serie",
                "numero_inventario",
                "motivo",
            }
        ),
    ),
}

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
MAILMERGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/mailMergeSource"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


W_P, W_R, W_T, W_TR, W_TC, W_TCPR, W_RPR = (w(x) for x in ("p", "r", "t", "tr", "tc", "tcPr", "rPr"))
W_FLDCHAR, W_INSTR, W_DELINSTR, W_FLDSIMPLE = (
    w(x) for x in ("fldChar", "instrText", "delInstrText", "fldSimple")
)
XML_SPACE = f"{{{XML_NS}}}space"

# Partes de texto donde se buscan campos y etiquetas.
TEXT_PART_RE = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
XML_DECL_RE = re.compile(rb"^\s*<\?xml[^>]*\?>")

# Etiquetas de docxtpl ya convertidas (se ignoran: así el script es idempotente).
JINJA_TOKEN_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}")
# Etiqueta de docxtemplater: {campo}, {#bucle}, {/bucle}; cualquier otra cosa entre llaves se rechaza.
DXT_TAG_RE = re.compile(r"\{([^{}]*)\}")
DXT_TAG_BODY_RE = re.compile(r"^([#/]?)\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")


class ConversionError(Exception):
    """La plantilla tiene algo que este script no sabe convertir con seguridad."""


@dataclass
class Report:
    merge_fields: int = 0
    tags: int = 0
    loops: list[str] = field(default_factory=list)
    mailmerge_removed: bool = False
    removed_parts: list[str] = field(default_factory=list)
    metadata_removed: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"campos MERGEFIELD quitados: {self.merge_fields}",
            f"etiquetas convertidas: {self.tags}",
            f"bucles: {', '.join(self.loops) if self.loops else 'ninguno'}",
            f"w:mailMerge quitado: {'sí' if self.mailmerge_removed else 'no (ya no estaba)'}",
        ]
        if self.removed_parts:
            out.append(f"partes eliminadas: {', '.join(self.removed_parts)}")
        out.append(
            "metadatos quitados: "
            + (", ".join(self.metadata_removed) if self.metadata_removed else "ninguno")
        )
        return out


# ---------------------------------------------------------------------------
# XML helpers


def _parse(data: bytes) -> etree._Element:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False, huge_tree=True)
    return etree.fromstring(data, parser)


def _serialize(root: etree._Element, original: bytes) -> bytes:
    """Serializa conservando la declaración XML original de la parte."""
    m = XML_DECL_RE.match(original)
    decl = m.group(0).strip() if m else b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    return decl + etree.tostring(root, encoding="UTF-8", xml_declaration=False)


def _ancestor(el: etree._Element, tag: str) -> etree._Element | None:
    cur = el.getparent()
    while cur is not None:
        if cur.tag == tag:
            return cur
        cur = cur.getparent()
    return None


def _paragraph_text(p: etree._Element) -> str:
    return "".join(t.text or "" for t in p.iter(W_T))


def _set_text(t: etree._Element, text: str) -> None:
    t.text = text
    t.set(XML_SPACE, "preserve")


def _drop_para_ids(el: etree._Element) -> None:
    for node in el.iter():
        for attr in (f"{{{W14_NS}}}paraId", f"{{{W14_NS}}}textId"):
            if attr in node.attrib:
                del node.attrib[attr]


# ---------------------------------------------------------------------------
# 1) Campos MERGEFIELD


def _remove_from_run(el: etree._Element, touched: set) -> None:
    parent = el.getparent()
    if parent is None:
        return
    parent.remove(el)
    if parent.tag == W_R:
        touched.add(parent)


def strip_merge_fields(root: etree._Element) -> int:
    """Quita los MERGEFIELD complejos y simples dejando el texto visible (el resultado del campo)."""
    removed = 0
    touched: set = set()

    # Campos simples: <w:fldSimple w:instr=" MERGEFIELD X "><w:r>…</w:r></w:fldSimple>
    for fs in list(root.iter(W_FLDSIMPLE)):
        if (fs.get(w("instr")) or "").strip().upper().startswith("MERGEFIELD"):
            parent = fs.getparent()
            idx = parent.index(fs)
            for child in list(fs):
                if child.tag == w("fldData"):
                    continue
                parent.insert(idx, child)
                idx += 1
            parent.remove(fs)
            removed += 1

    # Campos complejos: begin, instrText…, separate, resultado…, end
    fields = []
    stack: list[dict] = []
    for el in root.iter(W_FLDCHAR, W_INSTR, W_DELINSTR, W_T):
        if el.tag == W_FLDCHAR:
            kind = el.get(w("fldCharType"))
            if kind == "begin":
                if stack:
                    stack[-1]["nested"] = True
                stack.append({"begin": el, "instr": [], "sep": None, "nested": False, "t_in_code": False})
            elif kind == "separate":
                if not stack:
                    raise ConversionError("w:fldChar 'separate' sin 'begin'.")
                stack[-1]["sep"] = el
            elif kind == "end":
                if not stack:
                    raise ConversionError("w:fldChar 'end' sin 'begin'.")
                f = stack.pop()
                f["end"] = el
                fields.append(f)
        elif stack and stack[-1]["sep"] is None:
            if el.tag in (W_INSTR, W_DELINSTR):
                stack[-1]["instr"].append(el)
            elif el.tag == W_T and (el.text or ""):
                stack[-1]["t_in_code"] = True
    if stack:
        raise ConversionError("Hay un campo (w:fldChar 'begin') sin cerrar.")

    for f in fields:
        code = "".join(i.text or "" for i in f["instr"]).strip()
        if not code.upper().startswith("MERGEFIELD"):
            continue  # p. ej. PAGE o NUMPAGES: se conservan
        if f["nested"] or f["t_in_code"]:
            raise ConversionError(f"Campo MERGEFIELD con estructura no soportada: {code!r}")
        for el in [f["begin"], *f["instr"], f["sep"], f["end"]]:
            if el is not None:
                _remove_from_run(el, touched)
        removed += 1

    # Runs que quedaron vacíos (solo w:rPr) tras quitar los caracteres de campo.
    for r in touched:
        if all(child.tag == W_RPR for child in r) and r.getparent() is not None:
            r.getparent().remove(r)
    return removed


# ---------------------------------------------------------------------------
# 2) Etiquetas y bucles


@dataclass
class _Tag:
    t: etree._Element
    start: int
    end: int
    kind: str  # "open" | "close" | "var"
    name: str
    replacement: str | None = None
    order: int = 0


def _scan_tags(root: etree._Element) -> list[_Tag]:
    tags: list[_Tag] = []
    for t in root.iter(W_T):
        text = t.text or ""
        if "{" not in text and "}" not in text:
            continue
        masked = JINJA_TOKEN_RE.sub(lambda m: "\0" * len(m.group(0)), text)
        for m in DXT_TAG_RE.finditer(masked):
            body = DXT_TAG_BODY_RE.match(m.group(1))
            if not body:
                raise ConversionError(
                    f"Etiqueta no soportada {m.group(0)!r}: solo se convierten {{campo}}, "
                    "{#bucle} y {/bucle}."
                )
            prefix, name = body.groups()
            kind = {"#": "open", "/": "close", "": "var"}[prefix]
            tags.append(_Tag(t, m.start(), m.end(), kind, name))
    for i, tag in enumerate(tags):
        tag.order = i
    return tags


def _check_no_split_tags(root: etree._Element) -> None:
    """Una llave suelta en un párrafo indica una etiqueta partida entre runs (Word la parte así)."""
    for p in root.iter(W_P):
        leftover = []
        for t in p.iter(W_T):
            text = JINJA_TOKEN_RE.sub("", t.text or "")
            text = DXT_TAG_RE.sub("", text)
            leftover.append(text)
        joined = "".join(leftover)
        if "{" in joined or "}" in joined:
            raise ConversionError(
                "Hay una etiqueta partida entre varios runs o una llave suelta en el párrafo "
                f"{_paragraph_text(p)!r}. Ábrela en Word, reescribe la etiqueta de corrido y vuelve a "
                "correr el script."
            )


def _only_tag_in_paragraph(p: etree._Element, tag: _Tag) -> bool:
    raw = (tag.t.text or "")[tag.start : tag.end]
    return _paragraph_text(p).strip() == raw


def _marker_row(tr: etree._Element, text: str, rpr: etree._Element | None) -> etree._Element:
    """Copia de la fila con las celdas vacías y ``text`` en la primera (docxtpl la elimina)."""
    row = copy.deepcopy(tr)
    _drop_para_ids(row)
    for i, tc in enumerate(row.findall(W_TC)):
        for child in list(tc):
            if child.tag != W_TCPR:
                tc.remove(child)
        p = etree.SubElement(tc, W_P)
        if i == 0:
            r = etree.SubElement(p, W_R)
            if rpr is not None:
                r.append(copy.deepcopy(rpr))
            _set_text(etree.SubElement(r, W_T), text)
    return row


def convert_tags(root: etree._Element, loops: dict[str, tuple[str, frozenset[str]]], *, allow_loops: bool) -> tuple[int, list[str]]:
    _check_no_split_tags(root)
    tags = _scan_tags(root)
    if not tags:
        return 0, []

    # Emparejar bucles.
    pairs: list[tuple[_Tag, _Tag]] = []
    stack: list[_Tag] = []
    for tag in tags:
        if tag.kind == "open":
            if stack:
                raise ConversionError(f"Bucles anidados no soportados ({{#{stack[-1].name}}} y {{#{tag.name}}}).")
            stack.append(tag)
        elif tag.kind == "close":
            if not stack or stack[-1].name != tag.name:
                raise ConversionError(f"{{/{tag.name}}} no corresponde con ninguna apertura.")
            pairs.append((stack.pop(), tag))
    if stack:
        raise ConversionError(f"{{#{stack[-1].name}}} sin cierre.")
    if pairs and not allow_loops:
        raise ConversionError("Solo se soportan bucles en el cuerpo del documento.")

    described: list[str] = []
    scope: dict[int, str] = {}  # order -> variable del bucle
    for open_tag, close_tag in pairs:
        if open_tag.name not in loops:
            raise ConversionError(
                f"Bucle {{#{open_tag.name}}} sin configuración: indica su variable y campos con "
                f"--loop {open_tag.name}=var:campo1,campo2"
            )
        var, item_fields = loops[open_tag.name]
        name = open_tag.name
        p_open, p_close = _ancestor(open_tag.t, W_P), _ancestor(close_tag.t, W_P)
        tr_open, tr_close = _ancestor(open_tag.t, W_TR), _ancestor(close_tag.t, W_TR)

        if p_open is p_close:
            open_tag.replacement = f"{{% for {var} in {name} %}}"
            close_tag.replacement = "{% endfor %}"
            described.append(f"{name} (en línea)")
        elif (
            _only_tag_in_paragraph(p_open, open_tag)
            and _only_tag_in_paragraph(p_close, close_tag)
            and p_open.getparent() is p_close.getparent()
        ):
            open_tag.replacement = f"{{%p for {var} in {name} %}}"
            close_tag.replacement = "{%p endfor %}"
            described.append(f"{name} (párrafos)")
        elif tr_open is not None and tr_open is tr_close:
            open_rpr = open_tag.t.getparent().find(W_RPR)
            tr_open.addprevious(_marker_row(tr_open, f"{{%tr for {var} in {name} %}}", open_rpr))
            tr_open.addnext(_marker_row(tr_open, "{%tr endfor %}", open_rpr))
            open_tag.replacement = ""
            close_tag.replacement = ""
            described.append(f"{name} (fila de tabla)")
        else:
            raise ConversionError(
                f"No sé convertir el bucle {{#{name}}}: la apertura y el cierre deben estar en el mismo "
                "párrafo, solos en sus propios párrafos, o en la misma fila de una tabla."
            )
        for tag in tags:
            if open_tag.order < tag.order < close_tag.order and tag.kind == "var" and tag.name in item_fields:
                scope[tag.order] = var

    for tag in tags:
        if tag.kind == "var":
            var = scope.get(tag.order)
            tag.replacement = f"{{{{ {var}.{tag.name} }}}}" if var else f"{{{{ {tag.name} }}}}"

    # Reescribir cada w:t de derecha a izquierda.
    by_t: dict[etree._Element, list[_Tag]] = {}
    for tag in tags:
        by_t.setdefault(tag.t, []).append(tag)
    for t, items in by_t.items():
        text = t.text or ""
        for tag in sorted(items, key=lambda x: x.start, reverse=True):
            text = text[: tag.start] + (tag.replacement or "") + text[tag.end :]
        _set_text(t, text)
    return len(tags), described


# ---------------------------------------------------------------------------
# 3) settings.xml, relaciones y metadatos


def clean_settings(data: bytes) -> tuple[bytes, bool]:
    root = _parse(data)
    found = root.findall(w("mailMerge"))
    for el in found:
        root.remove(el)
    return (_serialize(root, data), True) if found else (data, False)


def clean_settings_rels(data: bytes) -> bytes | None:
    """Quita las relaciones mailMergeSource; devuelve None si el .rels queda vacío."""
    root = _parse(data)
    rels = [r for r in root if r.get("Type") == MAILMERGE_REL]
    if not rels:
        return data
    for r in rels:
        root.remove(r)
    if len(root) == 0:
        return None
    return _serialize(root, data)


def clean_core(data: bytes) -> tuple[bytes, list[str]]:
    root = _parse(data)
    removed = []
    for tag, label in (
        (f"{{{DC_NS}}}creator", "dc:creator"),
        (f"{{{CP_NS}}}lastModifiedBy", "cp:lastModifiedBy"),
        (f"{{{CP_NS}}}lastPrinted", "cp:lastPrinted"),
    ):
        for el in root.findall(tag):
            root.remove(el)
            removed.append(label)
    return (_serialize(root, data), removed) if removed else (data, [])


def clean_app(data: bytes) -> tuple[bytes, list[str]]:
    root = _parse(data)
    cleared = []
    for name in ("Company", "Manager", "HyperlinkBase"):
        for el in root.findall(f"{{{EP_NS}}}{name}"):
            if (el.text or "").strip():
                el.text = ""
                cleared.append(f"app:{name}")
    return (_serialize(root, data), cleared) if cleared else (data, [])


# ---------------------------------------------------------------------------
# Paquete completo


def convert_docx(src: bytes, loops: dict[str, tuple[str, frozenset[str]]] | None = None) -> tuple[bytes, Report]:
    loops = DEFAULT_LOOPS if loops is None else loops
    report = Report()
    zin = zipfile.ZipFile(io.BytesIO(src))
    names = zin.namelist()
    if "word/document.xml" not in names:
        raise ConversionError("El archivo no es un DOCX (falta word/document.xml).")

    out: dict[str, bytes | None] = {}
    for name in names:
        data = zin.read(name)
        if TEXT_PART_RE.match(name):
            root = _parse(data)
            n_fields = strip_merge_fields(root)
            n_tags, described = convert_tags(root, loops, allow_loops=name == "word/document.xml")
            report.merge_fields += n_fields
            report.tags += n_tags
            report.loops += described
            if n_fields or n_tags:
                data = _serialize(root, data)
        elif name == "word/settings.xml":
            data, removed = clean_settings(data)
            report.mailmerge_removed = report.mailmerge_removed or removed
        elif name == "word/_rels/settings.xml.rels":
            data = clean_settings_rels(data)
            if data is None:
                report.removed_parts.append(name)
        elif name == "docProps/core.xml":
            data, removed = clean_core(data)
            report.metadata_removed += removed
        elif name == "docProps/app.xml":
            data, removed = clean_app(data)
            report.metadata_removed += removed
        out[name] = data

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = out[info.filename]
            if data is None:
                continue
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zout.writestr(zi, data)
    return buf.getvalue(), report


def docx_entries(content: bytes) -> list[tuple[str, bytes]]:
    """Contenido descomprimido (nombre, bytes) en orden: para comparar sin depender de zlib."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return [(i.filename, z.read(i.filename)) for i in z.infolist()]


def _parse_loop(spec: str) -> tuple[str, tuple[str, frozenset[str]]]:
    m = re.fullmatch(r"([A-Za-z_]\w*)=([A-Za-z_]\w*):([A-Za-z_]\w*(?:,[A-Za-z_]\w*)*)", spec.strip())
    if not m:
        raise argparse.ArgumentTypeError("formato: bucle=var:campo1,campo2")
    return m.group(1), (m.group(2), frozenset(m.group(3).split(",")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT, help="plantilla docxtemplater")
    ap.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT, help="plantilla docxtpl a escribir")
    ap.add_argument("--check", action="store_true", help="no escribe; sale con 1 si la salida no está al día")
    ap.add_argument(
        "--loop",
        action="append",
        type=_parse_loop,
        default=[],
        metavar="BUCLE=VAR:CAMPOS",
        help="configura un bucle (por defecto: equipos=eq:num_dictamen,tipo_equipo,marca,…)",
    )
    args = ap.parse_args(argv)

    loops = dict(DEFAULT_LOOPS)
    loops.update(dict(args.loop))
    try:
        result, report = convert_docx(args.input.read_bytes(), loops)
    except zipfile.BadZipFile:
        print(f"ERROR: {args.input} no es un DOCX válido (no es un archivo ZIP).", file=sys.stderr)
        return 2
    except (ConversionError, etree.XMLSyntaxError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if args.output.exists() and docx_entries(args.output.read_bytes()) == docx_entries(result):
            print(f"OK: {args.output} está al día.")
            return 0
        print(f"DESACTUALIZADA: {args.output} no corresponde a {args.input}; vuelve a correr el script.")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result)
    print(f"{args.input} -> {args.output} ({len(result):,} bytes)")
    for line in report.lines():
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
