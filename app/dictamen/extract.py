"""Lectura del oficio (PDF, DOCX o foto) con el LLM local (Qwen vía Ollama, API OpenAI).

Sustituye a extract.js (Gemini). Cómo se prepara cada archivo:

- DOCX: texto con python-docx, en orden: párrafos, cuadros de texto, encabezado y tablas como
  filas ``celda | celda`` (las celdas combinadas verticalmente repiten su valor). Si el Word
  solo trae una imagen escaneada, se envían sus imágenes.
- PDF con capa de texto suficiente: texto con pymupdf; las tablas detectadas se escriben como
  filas ``celda | celda`` (así una celda vacía no desplaza las columnas).
- PDF escaneado (poco texto o una imagen que cubre la página): hasta ``max_pages`` páginas a
  PNG de ~150 dpi (lado mayor <= 2000 px).
- JPG/PNG: la foto se gira según EXIF y se reduce a <= 2000 px; se rechazan imágenes de más
  de 50 megapíxeles antes de decodificarlas (bombas de descompresión).

Ollama no le muestra al modelo las ``description`` del schema: las reglas por campo van EN EL
PROMPT (fecha con mes en letra, remitente = quien firma, identificadores carácter por carácter,
celda vacía = null, no inventar). ``response_format`` restringe la forma del JSON y además se
acepta JSON dentro de bloques ```json por si el servidor no lo respeta.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

import httpx

from .schema import GENERAL_NAMES, normalize_extraction

log = logging.getLogger(__name__)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JPEG_MIME = "image/jpeg"
PNG_MIME = "image/png"
SUPPORTED: dict[str, str] = {PDF_MIME: ".pdf", DOCX_MIME: ".docx", JPEG_MIME: ".jpg", PNG_MIME: ".png"}

MAX_IMAGE_SIDE = 2000
MAX_IMAGE_PIXELS = 50_000_000
RENDER_DPI = 150
MIN_TEXT_CHARS_PER_PAGE = 100  # letras y dígitos por página (promedio); menos = escaneado
SCAN_COVERAGE = 0.6  # una imagen que cubre >= 60 % de la página = página escaneada
MAX_TEXT_CHARS = 60_000
MAX_ZIP_ENTRIES = 5_000
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024
MIN_DOCX_IMAGE_SIDE = 800  # imágenes menores en un Word son logotipos, no el oficio escaneado
MAX_OUTPUT_TOKENS = 12_000

EQUIPO_EXTRACT_FIELDS = ["tipo_equipo", "marca", "modelo", "numero_serie", "numero_inventario"]


def _nullable() -> dict[str, Any]:
    return {"type": ["string", "null"]}


EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{name: _nullable() for name in GENERAL_NAMES},
        "equipos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {name: _nullable() for name in EQUIPO_EXTRACT_FIELDS},
                "required": EQUIPO_EXTRACT_FIELDS,
                "additionalProperties": False,
            },
        },
    },
    "required": [*GENERAL_NAMES, "equipos"],
    "additionalProperties": False,
}

PROMPT = """Extrae los datos de este oficio y de su tabla de equipos. Responde solo con el objeto JSON.

Reglas por campo:
- oficio_recibido: número del oficio (lo que sigue a "OFICIO NÚM.", "OFICIO No." u "Oficio número"), copiado carácter por carácter con sus letras, números, diagonales y guiones, p. ej. "FGEO/CGSI/1429/2025". No uses números de expediente, de carpeta ni de otros oficios citados en el texto.
- fecha_oficio_recibido: fecha del oficio (suele ir junto al lugar, p. ej. "Oaxaca de Juárez, Oax., a ..."), en MAYÚSCULAS y con el mes en letra, p. ej. "20 DE AGOSTO DEL 2025". Nunca la escribas como 2025-08-20 ni como 20/08/2025.
- nombre_remitente: la persona que FIRMA el oficio (su nombre va al final, después de "ATENTAMENTE"), con su título o grado abreviado si aparece, p. ej. "MTRA. ARIMA OJEDA SANTIAGO". No es el destinatario (a quien va dirigido, "PRESENTE") ni las personas en copia (C.c.p.).
- cargo_remitente: el cargo de quien firma, tal como aparece bajo su nombre.
- equipos: una entrada por cada equipo (fila) de la tabla o lista de bienes, en el mismo orden, sin la fila de encabezados ni filas vacías.
  - tipo_equipo, marca y modelo: como aparecen.
  - numero_serie y numero_inventario: copiados carácter por carácter, sin agregar, quitar ni cambiar caracteres; distingue con cuidado O de 0, I de 1, Q de O, S de 5 y B de 8.
  - Si la celda está vacía o solo tiene un guion, usa null.
- Si un dato no aparece explícitamente en el documento, usa null. No inventes ni completes valores."""


class ExtractionError(Exception):
    """Error con mensaje apto para mostrarse a la persona (sin detalles internos)."""


def sniff_mime(content: bytes, filename: str | None = None) -> str | None:
    """Tipo real del archivo por sus bytes mágicos (el nombre y el mime del cliente no cuentan).

    DOCX = ZIP que contiene ``word/document.xml``. Devuelve None si no es PDF, DOCX, JPG ni PNG.
    """
    if not content:
        return None
    if content.startswith(b"%PDF-") or b"%PDF-" in content[:1024]:
        return PDF_MIME
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG_MIME
    if content.startswith(b"\xff\xd8\xff"):
        return JPEG_MIME
    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                if "word/document.xml" in zf.namelist():
                    return DOCX_MIME
        except Exception:  # noqa: BLE001 - ZIP dañado o no soportado
            return None
    return None


@dataclass
class PreparedOficio:
    mode: str  # "texto" | "imagen"
    pages: int
    total_pages: int
    text: str | None = None
    images: list[tuple[str, bytes]] = field(default_factory=list)  # (mime, bytes)

    def content_parts(self) -> list[dict[str, Any]]:
        if self.mode == "texto":
            parts = [{"type": "text", "text": self.text or ""}]
        else:
            parts = []
            for i, (mime, data) in enumerate(self.images, start=1):
                if len(self.images) > 1:
                    parts.append({"type": "text", "text": f"Página {i} de {len(self.images)}:"})
                url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
        parts.append({"type": "text", "text": PROMPT})
        return parts


# --------------------------------------------------------------------------- imágenes

def prepare_image(content: bytes) -> tuple[str, bytes]:
    """Gira según EXIF, aplana transparencias y reduce a <= MAX_IMAGE_SIDE px.

    Devuelve el archivo original si no hace falta ningún cambio.
    """
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(content))
        width, height = img.size
    except Image.DecompressionBombError as exc:
        raise ExtractionError("La imagen es demasiado grande. Sube una foto de menor resolución.") from exc
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("No pude abrir la imagen; puede estar dañada.") from exc
    if width * height > MAX_IMAGE_PIXELS:
        raise ExtractionError(
            f"La imagen es demasiado grande ({width}×{height} px). Sube una foto de menor resolución."
        )
    fmt = img.format
    try:
        orientation = img.getexif().get(0x0112, 1)
        if (
            fmt in ("JPEG", "PNG") and max(width, height) <= MAX_IMAGE_SIDE
            and orientation in (None, 1) and img.mode in ("RGB", "L")
        ):
            return (JPEG_MIME if fmt == "JPEG" else PNG_MIME), content
        if fmt == "JPEG" and max(width, height) > MAX_IMAGE_SIDE:
            img.draft("RGB", (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))  # decodifica ya reducida
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            img = background
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if fmt == "PNG":
            img.save(out, format="PNG", optimize=False)
            return PNG_MIME, out.getvalue()
        img.save(out, format="JPEG", quality=90)
        return JPEG_MIME, out.getvalue()
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("No pude procesar la imagen; puede estar dañada.") from exc


# --------------------------------------------------------------------------- PDF

def _alnum(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def _intersection_ratio(inner, outer) -> float:
    x0, y0 = max(inner[0], outer[0]), max(inner[1], outer[1])
    x1, y1 = min(inner[2], outer[2]), min(inner[3], outer[3])
    area = max(0.0, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    if area <= 0 or x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / area


def _covered_by_image(page) -> bool:
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return False
    return any(_intersection_ratio(page.rect, info["bbox"]) >= SCAN_COVERAGE for info in infos)


def _needs_render(pages) -> bool:
    """PDF escaneado: poco texto en promedio (también si el texto se convirtió en curvas) o
    alguna página cubierta por una imagen (escaneo con una capa de texto parcial, p. ej. la
    cadena de una firma electrónica). Una última hoja corta (solo la firma) no basta."""
    total = sum(_alnum(p.get_text("text")) for p in pages)
    if total < MIN_TEXT_CHARS_PER_PAGE * len(pages):
        return True
    return any(_covered_by_image(p) for p in pages)


def _cell_text(page, bbox) -> str:
    import pymupdf

    if bbox is None:
        return ""
    return " ".join(page.get_text("text", clip=pymupdf.Rect(bbox)).split())


def _table_cells(page, table) -> list[list[str]]:
    """Texto de cada celda. Una celda ``None`` (cubierta por una combinada) hereda el texto
    de la celda de arriba si esta abarca la fila; si no (combinación horizontal), queda vacía."""
    out: list[list[str]] = []
    masters: dict[int, tuple[Any, str]] = {}  # columna -> (bbox, texto) de la última celda real
    for row in table.rows:
        cells: list[str] = []
        for col, bbox in enumerate(row.cells):
            if bbox is not None:
                text = _cell_text(page, bbox)
                masters[col] = (bbox, text)
            else:
                above = masters.get(col)
                text = above[1] if above and above[0][3] >= row.bbox[3] - 1 else ""
            cells.append(text)
        out.append(cells)
    return out


def pdf_page_text(page) -> str:
    """Texto de la página; cada tabla detectada se escribe como filas ``celda | celda``.

    El texto de cada celda se toma con ``get_text(clip=...)`` porque ``Table.extract()`` de
    pymupdf puede perder espacios y mover guiones (FGEO-CPU-004512 -> "FGEOCPU004512 - -").
    """
    blocks = [b for b in page.get_text("blocks") if b[6] == 0]
    tables: list[tuple[tuple[float, float, float, float], list[str]]] = []
    try:
        for table in page.find_tables().tables:
            rows = [" | ".join(cells) for cells in _table_cells(page, table)]
            if len(rows) >= 2 and table.col_count >= 2 and any(r.strip(" |") for r in rows):
                tables.append((tuple(table.bbox), rows))
    except Exception:  # noqa: BLE001 - la detección de tablas es heurística
        log.debug("find_tables falló", exc_info=True)
        tables = []
    lines: list[str] = []
    emitted: set[int] = set()
    for block in blocks:
        owner = next(
            (i for i, (bbox, _) in enumerate(tables) if _intersection_ratio(block[:4], bbox) > 0.5),
            None,
        )
        if owner is None:
            lines.append(block[4].strip())
        elif owner not in emitted:
            emitted.add(owner)
            lines.extend(["[Tabla]", *tables[owner][1], "[Fin de tabla]"])
    for i, (_, rows) in enumerate(tables):
        if i not in emitted:
            lines.extend(["[Tabla]", *rows, "[Fin de tabla]"])
    return "\n".join(line for line in lines if line)


def _render_page(page) -> bytes:
    import pymupdf

    longest = max(page.rect.width, page.rect.height, 1.0)
    zoom = min(RENDER_DPI / 72.0, MAX_IMAGE_SIDE / longest)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def prepare_pdf(content: bytes, max_pages: int) -> PreparedOficio:
    import pymupdf

    try:
        doc = pymupdf.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("No pude abrir el PDF; puede estar dañado.") from exc
    with doc:
        if doc.needs_pass:
            raise ExtractionError(
                "El PDF está protegido con contraseña. Quita la protección o sube una foto del oficio."
            )
        total = doc.page_count
        if total == 0:
            raise ExtractionError("El PDF no tiene páginas.")
        pages = [doc[i] for i in range(min(total, max_pages))]
        try:
            if not _needs_render(pages):
                parts = []
                for i, page in enumerate(pages, start=1):
                    header = f"--- Página {i} ---\n" if len(pages) > 1 else ""
                    parts.append(header + pdf_page_text(page))
                text = _limit_text("\n\n".join(parts))
                return PreparedOficio("texto", len(pages), total, text=_text_header("PDF") + text)
            images = [(PNG_MIME, _render_page(p)) for p in pages]
        except ExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError("No pude leer el contenido del PDF; puede estar dañado.") from exc
    return PreparedOficio("imagen", len(images), total, images=images)


# --------------------------------------------------------------------------- DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_SKIP_INLINE = {_W + "pPr", _W + "rPr", _W + "moveFrom", _W + "del", _MC_FALLBACK, _W + "sectPr"}


def _inline_text(el, boxes: list) -> str:
    out: list[str] = []
    for child in el:
        tag = child.tag
        if not isinstance(tag, str) or tag in _SKIP_INLINE:
            continue
        if tag == _W + "t":
            out.append(child.text or "")
        elif tag == _W + "tab":
            out.append("\t")
        elif tag in (_W + "br", _W + "cr"):
            out.append("\n")
        elif tag == _W + "noBreakHyphen":
            out.append("-")
        elif tag == _W + "txbxContent":
            boxes.append(child)
        else:
            out.append(_inline_text(child, boxes))
    return "".join(out)


def _cell_lines(tc) -> str:
    lines: list[str] = []
    _docx_blocks(tc, lines, nested=True)
    return " ".join(" ".join(lines).split())


_WRAPPERS = {_W + "sdt", _W + "sdtContent", _W + "customXml"}


def _children(el, tag: str):
    """Hijos directos con ``tag``, atravesando controles de contenido (w:sdt, w:customXml)."""
    for child in el:
        if child.tag == tag:
            yield child
        elif child.tag in _WRAPPERS:
            yield from _children(child, tag)


def _int_val(el, default: int) -> int:
    try:
        return max(0, int(el.get(_W + "val", default))) if el is not None else default
    except ValueError:
        return default


def _docx_table(tbl, lines: list[str], nested: bool) -> None:
    above: dict[int, str] = {}  # columna de la cuadrícula -> texto (para celdas combinadas)
    rows: list[str] = []
    for tr in _children(tbl, _W + "tr"):
        trpr = tr.find(_W + "trPr")
        col = _int_val(trpr.find(_W + "gridBefore") if trpr is not None else None, 0)
        cells: list[str] = []
        for tc in _children(tr, _W + "tc"):
            tcpr = tc.find(_W + "tcPr")
            span, vmerge = 1, None
            if tcpr is not None:
                span = max(1, _int_val(tcpr.find(_W + "gridSpan"), 1))
                vm = tcpr.find(_W + "vMerge")
                if vm is not None:
                    vmerge = vm.get(_W + "val", "continue")
            text = above.get(col, "") if vmerge == "continue" else _cell_lines(tc)
            above[col] = text
            cells.append(text)
            col += span
        rows.append(" | ".join(cells))
    if nested:
        lines.append(" ; ".join(rows))
    else:
        lines.extend(["[Tabla]", *rows, "[Fin de tabla]"])


def _docx_blocks(container, lines: list[str], nested: bool = False) -> None:
    for child in container:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag == _W + "p":
            boxes: list = []
            text = _inline_text(child, boxes)
            text = "\n".join(" ".join(part.split()) for part in text.split("\n")).strip()
            if text:
                lines.append(text)
            for box in boxes:
                _docx_blocks(box, lines, nested)
        elif tag == _W + "tbl":
            _docx_table(child, lines, nested)
        elif tag in (_W + "sdt", _W + "customXml"):
            content = child.find(_W + "sdtContent") if tag == _W + "sdt" else child
            if content is not None:
                _docx_blocks(content, lines, nested)


def _docx_parts(content: bytes) -> tuple[str, str]:
    import docx

    try:
        document = docx.Document(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("No pude leer el archivo Word (.docx); puede estar dañado.") from exc
    header_lines: list[str] = []
    seen: set[str] = set()
    for section in document.sections:
        for hdr in (section.first_page_header, section.header):
            try:
                if hdr.is_linked_to_previous:  # sin definición propia (leerla la crearía)
                    continue
                lines: list[str] = []
                _docx_blocks(hdr._element, lines)
            except Exception:  # noqa: BLE001
                continue
            text = "\n".join(lines)
            if text and text not in seen:
                seen.add(text)
                header_lines.append(text)
    body: list[str] = []
    _docx_blocks(document.element.body, body)
    header = ""
    if header_lines:
        header = "[Encabezado de página]\n" + "\n".join(header_lines) + "\n[Fin de encabezado]"
    return header, "\n".join(body).strip()


def docx_to_text(content: bytes) -> str:
    """Texto del DOCX en orden de lectura: encabezado, párrafos, cuadros de texto y tablas
    como filas ``celda | celda`` (entre [Tabla] y [Fin de tabla])."""
    header, body = _docx_parts(content)
    return "\n".join(p for p in (header, body) if p)


def _docx_pages(zf: zipfile.ZipFile) -> int:
    try:
        app = zf.read("docProps/app.xml").decode("utf-8", "replace")
    except KeyError:
        return 1
    m = re.search(r"<(?:\w+:)?Pages>(\d+)</", app)
    return max(1, int(m[1])) if m else 1


def _docx_images(zf: zipfile.ZipFile, max_pages: int) -> list[tuple[str, bytes]]:
    from PIL import Image

    def natural(name: str):
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]

    out = []
    for name in sorted((n for n in zf.namelist() if n.startswith("word/media/")), key=natural):
        data = zf.read(name)
        if sniff_mime(data) not in (JPEG_MIME, PNG_MIME):
            continue
        try:
            if max(Image.open(io.BytesIO(data)).size) < MIN_DOCX_IMAGE_SIDE:
                continue
        except Exception:  # noqa: BLE001
            continue
        out.append(prepare_image(data))
        if len(out) >= max_pages:
            break
    return out


def prepare_docx(content: bytes, max_pages: int) -> PreparedOficio:
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("No pude leer el archivo Word (.docx); puede estar dañado.") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_ZIP_UNCOMPRESSED:
            raise ExtractionError("El archivo Word es demasiado grande al descomprimirlo.")
        pages = _docx_pages(zf)
        header, body = _docx_parts(content)
        if _alnum(body) >= MIN_TEXT_CHARS_PER_PAGE:  # el membrete del encabezado no cuenta
            text = "\n".join(p for p in (header, body) if p)
            return PreparedOficio("texto", pages, pages, text=_text_header("DOCX") + _limit_text(text))
        images = _docx_images(zf, max_pages)
    if not images:
        raise ExtractionError("El archivo Word no tiene texto ni imágenes legibles del oficio.")
    return PreparedOficio("imagen", len(images), pages, images=images)


# --------------------------------------------------------------------------- común

def _text_header(kind: str) -> str:
    return (
        f"CONTENIDO DEL OFICIO (texto extraído de un archivo {kind}; las tablas aparecen entre "
        "[Tabla] y [Fin de tabla], una fila por línea y las celdas separadas por \" | \"):\n"
    )


def _limit_text(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    log.warning("Texto del oficio recortado de %d a %d caracteres", len(text), MAX_TEXT_CHARS)
    return text[:MAX_TEXT_CHARS] + "\n[... texto recortado ...]"


def prepare(content: bytes, mime: str, max_pages: int = 4) -> PreparedOficio:
    if mime == DOCX_MIME:
        return prepare_docx(content, max_pages)
    if mime == PDF_MIME:
        return prepare_pdf(content, max_pages)
    if mime in (JPEG_MIME, PNG_MIME):
        return PreparedOficio("imagen", 1, 1, images=[prepare_image(content)])
    raise ExtractionError("Formato no reconocido. Sube un PDF, un Word (.docx) o una foto (JPG o PNG).")


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def parse_json_response(text: Any) -> dict[str, Any]:
    """JSON de la respuesta del modelo; tolera bloques ```json y texto alrededor."""
    if isinstance(text, list):  # algunos servidores devuelven partes
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if not isinstance(text, str) or not text.strip():
        raise ExtractionError("El modelo no devolvió datos. Intenta de nuevo o captura los datos a mano.")
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE.findall(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ExtractionError(
        "El modelo no devolvió datos legibles. Intenta de nuevo o captura los datos a mano."
    )


class OficioExtractor:
    """Extrae los datos del oficio con un servidor compatible con OpenAI (Ollama /v1).

    Las llamadas al modelo se hacen de una en una (el servidor local procesa una petición a la
    vez y así el tiempo límite cuenta desde que la petición realmente se envía).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        timeout_s: float = 240,
        max_pages: int = 4,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_s = float(timeout_s)
        self.max_pages = max(1, int(max_pages))
        self.transport = transport
        self._lock = asyncio.Lock()

    def request_body(self, prepared: PreparedOficio) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": prepared.content_parts()}],
            "stream": False,
            "temperature": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "reasoning_effort": "none",
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "oficio", "strict": True, "schema": EXTRACT_SCHEMA},
            },
        }

    async def _call(self, body: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self.timeout_s, connect=min(15.0, self.timeout_s))
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as client:
                resp = await asyncio.wait_for(
                    client.post(
                        f"{self.base_url}/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    ),
                    timeout=self.timeout_s + 5,  # tope total aunque el servidor gotee bytes
                )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ExtractionError(
                f"El modelo tardó más de {self.timeout_s:.0f} s en leer el oficio. "
                "Intenta de nuevo o captura los datos a mano."
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("No se pudo conectar con el LLM para extraer el oficio: %s", exc)
            raise ExtractionError(
                "No pude conectarme con el modelo de lenguaje para leer el oficio. "
                "Intenta de nuevo en un momento o captura los datos a mano."
            ) from exc
        if resp.status_code >= 400:
            log.warning("El LLM respondió %s al extraer el oficio: %s", resp.status_code, resp.text[:300])
            raise ExtractionError(
                f"El modelo de lenguaje respondió con un error (HTTP {resp.status_code}). "
                "Intenta de nuevo en un momento o captura los datos a mano."
            )
        try:
            payload = resp.json()
            choice = payload["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ExtractionError(
                "El modelo no devolvió datos legibles. Intenta de nuevo o captura los datos a mano."
            ) from exc
        if choice.get("finish_reason") == "length":
            raise ExtractionError(
                "La respuesta del modelo quedó incompleta (¿demasiados equipos?). "
                "Intenta con menos páginas o captura los datos a mano."
            )
        raw = parse_json_response(message.get("content"))
        log.info("Oficio extraído; uso de tokens: %s", payload.get("usage"))
        return raw

    async def extract(self, content: bytes, filename: str) -> dict[str, Any]:
        """Lee el oficio y devuelve ``{"data", "mode", "pages", "total_pages", "elapsed_s"}``.

        ``data`` ya viene normalizado (ver ``schema.normalize_extraction``). Cualquier fallo
        (archivo ilegible, red, tiempo agotado, JSON inválido) es ``ExtractionError``.
        """
        started = time.perf_counter()
        mime = sniff_mime(content, filename)
        if mime is None:
            raise ExtractionError(
                "Formato no reconocido. Sube un PDF, un Word (.docx) o una foto (JPG o PNG)."
            )
        prepared = await asyncio.to_thread(prepare, content, mime, self.max_pages)
        body = self.request_body(prepared)
        async with self._lock:
            raw = await self._call(body)
        data = normalize_extraction(raw)
        elapsed = round(time.perf_counter() - started, 2)
        log.info(
            "Oficio leído en %.1f s: modo=%s páginas=%d/%d equipos=%d",
            elapsed, prepared.mode, prepared.pages, prepared.total_pages, len(data["equipos"]),
        )
        return {
            "data": data,
            "mode": prepared.mode,
            "pages": prepared.pages,
            "total_pages": prepared.total_pages,
            "elapsed_s": elapsed,
        }
