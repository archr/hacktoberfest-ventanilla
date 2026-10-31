"""Campos del dictamen técnico, normalización de lo extraído y cálculo de faltantes.

Equivale a ``camposFaltantes`` (extract.js) + ``OWN_FIELDS``/``EJEMPLOS`` (index.js) del bot
original, con estas diferencias deliberadas:

- Los equipos siempre tienen exactamente las claves de ``EQUIPO_FIELDS`` (nada de
  ``equipos: "3"`` ni claves inventadas como en ``applyCorrections``).
- Sin equipos no se puede generar: ``faltantes`` devuelve ``"equipos"``.
- ``dictamen_inicial`` debe ser solo dígitos (el original imprimía ``NaN``).
- ``motivo`` general solo es obligatorio si algún equipo no tiene motivo propio.
- Las fechas ISO, numéricas o "3 de septiembre de 2026" se unifican a ``03 DE SEPTIEMBRE DEL
  2026`` (Qwen devuelve ISO si no se le indica lo contrario) y se quitan los caracteres que no
  admite XML 1.0.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from datetime import date
from typing import Any

MAX_EQUIPOS = 100
MAX_FIELD_LEN = 500  # el endpoint de generación rechaza cadenas más largas

GENERAL_FIELDS: list[dict[str, Any]] = [
    {
        "name": "oficio_recibido",
        "label": "Número del oficio recibido",
        "required": True,
        "example": "FGEO/DA/0532/2026",
        "help": "Tal como aparece en el oficio que pide el dictamen.",
    },
    {
        "name": "fecha_oficio_recibido",
        "label": "Fecha del oficio recibido",
        "required": True,
        "example": "20 DE AGOSTO DEL 2025",
        "help": "Con el mes en letra.",
    },
    {
        "name": "cargo_remitente",
        "label": "Cargo de quien firma el oficio",
        "required": True,
        "example": "DIRECTORA ADMINISTRATIVA",
        "help": "",
    },
    {
        "name": "nombre_remitente",
        "label": "Nombre de quien firma el oficio",
        "required": True,
        "example": "MTRA. ARIMA OJEDA SANTIAGO",
        "help": "Con título o grado.",
    },
]

EQUIPO_FIELDS: list[dict[str, Any]] = [
    {"name": "tipo_equipo", "label": "Tipo de equipo", "required": True,
     "example": "COMPUTADORA DE ESCRITORIO", "help": ""},
    {"name": "marca", "label": "Marca", "required": True, "example": "HP", "help": ""},
    {"name": "modelo", "label": "Modelo", "required": True, "example": "PRODESK 400 G7", "help": ""},
    {"name": "numero_serie", "label": "Número de serie", "required": True,
     "example": "MXL1234ABC", "help": ""},
    {"name": "numero_inventario", "label": "Número de inventario", "required": False,
     "example": "FGEO-CPU-004512", "help": "Si no tiene, se imprime S/N."},
    {"name": "motivo", "label": "Motivo propio", "required": False,
     "example": "EQUIPO CON FALLA DE ENCENDIDO",
     "help": "Solo si es distinto del motivo general."},
]

# Ejemplos de EJEMPLOS en index.js.
OWN_FIELDS: list[dict[str, Any]] = [
    {"name": "numero_oficio", "label": "Número de oficio del dictamen", "required": True,
     "example": "FGEO/CGSI/1429/2025", "help": "El oficio de respuesta de la CGSI."},
    {"name": "dictamen_inicial", "label": "Número del primer dictamen", "required": True,
     "example": "191", "help": "Solo dígitos; cada equipo recibe el siguiente número."},
    {"name": "iniciales", "label": "Iniciales del técnico", "required": True,
     "example": "igf/dsa", "help": "Varios técnicos separados por / o coma."},
    {"name": "motivo", "label": "Motivo del dictamen", "required": True,
     "example": "EQUIPO CON FALLA DE ENCENDIDO",
     "help": "Obligatorio solo si algún equipo no tiene motivo propio."},
]

GENERAL_NAMES = [f["name"] for f in GENERAL_FIELDS]
EQUIPO_NAMES = [f["name"] for f in EQUIPO_FIELDS]
OWN_NAMES = [f["name"] for f in OWN_FIELDS]
EQUIPO_REQUIRED = [f["name"] for f in EQUIPO_FIELDS if f["required"]]

MESES = ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO",
         "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"]

# Caracteres prohibidos en XML 1.0 (Word no abre el documento si aparecen) y sustitutos sueltos.
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_WS = re.compile(r"\s+")
_EMPTY_WORDS = {"null", "none", "n/a"}
_ONLY_DASHES = re.compile(r"[-_.–—]+")
_DIGITS = re.compile(r"[0-9]+")
_ISO_DATE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?")
_DMY_DATE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")
_TEXT_DATE = re.compile(
    r"(\d{1,2})\s*[º°]?\s+DE\s+([A-Z]+)\s+DEL?\s+(?:AÑO\s+)?(\d{4})", re.IGNORECASE
)
_MES_ALIAS = {"SETIEMBRE": "SEPTIEMBRE"}


def clean_text(value: Any) -> str | None:
    """Convierte un valor del LLM/usuario en texto limpio o None.

    Quita caracteres inválidos en XML, colapsa espacios y saltos de línea, recorta a
    ``MAX_FIELD_LEN`` y trata "", "null", "N/A" o solo guiones como vacío. Números -> texto;
    booleanos, listas y objetos -> None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        value = str(int(value)) if value.is_integer() else str(value)
    elif not isinstance(value, str):
        return None
    text = _WS.sub(" ", _XML_INVALID.sub("", value)).strip()
    if not text or text.lower() in _EMPTY_WORDS or _ONLY_DASHES.fullmatch(text):
        return None
    return text[:MAX_FIELD_LEN].strip()


def _fecha_texto(year: int, month: int, day: int) -> str | None:
    try:
        date(year, month, day)
    except ValueError:
        return None
    return f"{day:02d} DE {MESES[month - 1]} DEL {year}"


def _sin_acentos(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def normalize_fecha(value: str | None) -> str | None:
    """'2026-08-20', '20/08/2026' o '3 de septiembre de 2026' -> 'DD DE MES DEL AAAA'.

    Todas las fechas quedan con el estilo de la fecha del propio dictamen (día con dos dígitos,
    "DEL"); el modelo a veces copia "DE 2026" y a veces "DEL 2026". Las fechas numéricas con
    día primero siguen el uso mexicano (DD/MM/AAAA). Una fecha imposible (32/13/2026) o con
    otro formato no se toca para que la persona la revise.
    """
    if not value:
        return value
    text = value.strip()
    if m := _ISO_DATE.fullmatch(text):
        return _fecha_texto(int(m[1]), int(m[2]), int(m[3])) or value
    if m := _DMY_DATE.fullmatch(text):
        return _fecha_texto(int(m[3]), int(m[2]), int(m[1])) or value
    if m := _TEXT_DATE.fullmatch(_sin_acentos(text)):
        mes = _MES_ALIAS.get(m[2].upper(), m[2].upper())
        if mes in MESES:
            return _fecha_texto(int(m[3]), MESES.index(mes) + 1, int(m[1])) or value
    return value


def _equipo(raw: Any) -> dict[str, str | None] | None:
    if not isinstance(raw, dict):
        return None
    eq = {name: clean_text(raw.get(name)) for name in EQUIPO_NAMES}
    return eq if any(eq.values()) else None


def normalize_extraction(raw: Any) -> dict[str, Any]:
    """Respuesta cruda del LLM -> ``data`` con forma fija.

    ``{"oficio_recibido", "fecha_oficio_recibido", "cargo_remitente", "nombre_remitente",
    "equipos": [{tipo_equipo, marca, modelo, numero_serie, numero_inventario, motivo}]}``.
    Tolera ``None``, tipos raros y claves sobrantes; descarta filas totalmente vacías y
    conserva como máximo ``MAX_EQUIPOS`` equipos.
    """
    src = raw if isinstance(raw, dict) else {}
    data: dict[str, Any] = {name: clean_text(src.get(name)) for name in GENERAL_NAMES}
    data["fecha_oficio_recibido"] = normalize_fecha(data["fecha_oficio_recibido"])
    raw_equipos = src.get("equipos")
    equipos: list[dict[str, str | None]] = []
    if isinstance(raw_equipos, list):
        for item in raw_equipos:
            eq = _equipo(item)
            if eq is not None:
                equipos.append(eq)
            if len(equipos) >= MAX_EQUIPOS:
                break
    data["equipos"] = equipos
    return data


def empty_draft() -> dict[str, Any]:
    return {
        "data": {**{name: None for name in GENERAL_NAMES}, "equipos": []},
        "own": {name: None for name in OWN_NAMES},
    }


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def dictamen_inicial_valido(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, str) and bool(_DIGITS.fullmatch(value.strip()))


def faltantes(data: dict[str, Any] | None, own: dict[str, Any] | None) -> list[str]:
    """Campos que faltan, con los nombres del bot original.

    Ejemplos: ``oficio_recibido``, ``equipos`` (lista vacía), ``equipo_2_marca``,
    ``numero_oficio``, ``dictamen_inicial`` (vacío o con algo que no sea dígito). ``motivo``
    solo aparece si algún equipo no tiene motivo propio.
    """
    data = data if isinstance(data, dict) else {}
    own = own if isinstance(own, dict) else {}
    out = [name for name in GENERAL_NAMES if _blank(data.get(name))]
    equipos = data.get("equipos")
    equipos = [e for e in equipos if isinstance(e, dict)] if isinstance(equipos, list) else []
    if not equipos:
        out.append("equipos")
    for i, eq in enumerate(equipos, start=1):
        out.extend(f"equipo_{i}_{name}" for name in EQUIPO_REQUIRED if _blank(eq.get(name)))
    for name in OWN_NAMES:
        value = own.get(name)
        if name == "dictamen_inicial":
            missing = not dictamen_inicial_valido(value)
        elif name == "motivo":
            missing = _blank(value) and any(_blank(eq.get("motivo")) for eq in equipos)
        else:
            missing = _blank(value)
        if missing:
            out.append(name)
    return out


def field_spec() -> dict[str, list[dict[str, Any]]]:
    """Definición de campos para la UI (copias: modificarlas no afecta al módulo)."""
    return {
        "general": copy.deepcopy(GENERAL_FIELDS),
        "equipo": copy.deepcopy(EQUIPO_FIELDS),
        "own": copy.deepcopy(OWN_FIELDS),
    }
