"""Generación del DOCX del dictamen técnico (port de ``generate.js`` de dictamen-bot a docxtpl).

El contexto de render es el mismo que arma ``generate.js`` (mayúsculas, ``frase_equipos``,
``otros_componentes='---'``, un elemento por equipo con ``num_dictamen`` consecutivo…), con estas
correcciones de errores del original:

* ``dictamen_inicial`` debe ser solo dígitos (el original imprimía «NaN» o descartaba sufijos) y
  se conservan los ceros a la izquierda que escriba la persona: ``0191`` -> ``0191``, ``0192``…
* Sin equipos, o con datos obligatorios vacíos, se lanza :class:`DictamenError` en lugar de
  generar un oficio incompleto.
* Un motivo terminado en punto no produce «..» (la plantilla ya agrega el punto).
* La fecha del oficio se calcula en America/Mexico_City, no en la zona del servidor.
* Se quitan los caracteres de control inválidos en XML y se normalizan los saltos de línea
  (``\\r\\n`` -> ``\\n``; docxtpl convierte ``\\n`` en salto de línea dentro del mismo párrafo).

La plantilla (``config/dictamen/dictamen.docx``) se genera con
``scripts/convert_dictamen_template.py`` a partir de la original de docxtemplater.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping

MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]  # fmt: skip
ZONA_HORARIA = "America/Mexico_City"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

GENERAL_REQUERIDOS = ("oficio_recibido", "fecha_oficio_recibido", "cargo_remitente", "nombre_remitente")
EQUIPO_REQUERIDOS = ("tipo_equipo", "marca", "modelo", "numero_serie")
OWN_REQUERIDOS = ("numero_oficio", "dictamen_inicial", "iniciales")

ETIQUETAS = {
    "oficio_recibido": "número del oficio recibido",
    "fecha_oficio_recibido": "fecha del oficio recibido",
    "cargo_remitente": "cargo del remitente",
    "nombre_remitente": "nombre del remitente",
    "numero_oficio": "número de oficio del dictamen",
    "dictamen_inicial": "número de dictamen inicial",
    "iniciales": "iniciales de los técnicos",
    "motivo": "motivo del dictamen",
    "equipos": "al menos un equipo",
    "tipo_equipo": "tipo",
    "marca": "marca",
    "modelo": "modelo",
    "numero_serie": "número de serie",
}

# Caracteres prohibidos en XML 1.0 (docxtpl/lxml fallarían o los perderían en silencio).
_XML_INVALIDOS = re.compile("[\x00-\x08\x0e-\x1f\ud800-\udfff￾￿]")
_SOLO_DIGITOS = re.compile(r"[0-9]+")
_PUNTO_FINAL = re.compile(r"[\s.]+$")


class DictamenError(ValueError):
    """Datos que impiden generar el dictamen; el mensaje es apto para mostrarse a la persona.

    ``faltantes`` usa los nombres de campo del bot ("oficio_recibido", "equipo_2_marca", "motivo"…).
    """

    def __init__(self, message: str, faltantes: list[str] | None = None):
        super().__init__(message)
        self.faltantes = list(faltantes or [])


class PlantillaError(RuntimeError):
    """La plantilla no existe o no se puede renderizar (problema del servidor, no de los datos)."""


# ---------------------------------------------------------------------------
# Fechas


def zona_mexico() -> tzinfo:
    """America/Mexico_City; si el sistema no trae tzdata, UTC-6 fijo (México sin horario de verano desde 2022)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(ZONA_HORARIA)
    except Exception:  # ZoneInfoNotFoundError o módulo sin datos
        return timezone(timedelta(hours=-6), "CST")


def hoy_mexico() -> date:
    return datetime.now(zona_mexico()).date()


def _como_fecha(d: date | datetime) -> date:
    if isinstance(d, datetime):
        return d.astimezone(zona_mexico()).date() if d.tzinfo else d.date()
    return d


def fecha_larga(d: date | datetime) -> str:
    """``05 de octubre del 2026`` (día a dos dígitos, mes en minúsculas), como ``fechaLarga`` del bot."""
    d = _como_fecha(d)
    return f"{d.day:02d} de {MESES[d.month - 1]} del {d.year}"


# ---------------------------------------------------------------------------
# Contexto


def _texto(valor: Any, campo: str) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        raise DictamenError(f"El campo «{ETIQUETAS.get(campo, campo)}» tiene un valor inválido.")
    texto = str(valor).replace("\r\n", "\n").replace("\r", "\n")
    texto = texto.replace("\x0b", "\n").replace("\x0c", "\n")  # salto manual de Word / form feed
    texto = _XML_INVALIDOS.sub("", texto)
    return texto.strip()


def _u(valor: str) -> str:
    return valor.upper()


def _num_dictamen(inicial: str, i: int) -> int | str:
    """``int(dictamen_inicial) + i``; si la persona escribió ceros a la izquierda, se conservan."""
    numero = int(inicial) + i
    if len(inicial) > 1 and inicial.startswith("0"):
        return str(numero).zfill(len(inicial))
    return numero


def build_context(data: Mapping[str, Any], own: Mapping[str, Any], today: date | datetime | None = None) -> dict:
    """Contexto de render equivalente al de ``generate.js``; lanza :class:`DictamenError` si falta algo."""
    if not isinstance(data, Mapping) or not isinstance(own, Mapping):
        raise DictamenError("Los datos del dictamen no tienen el formato esperado.")
    equipos_raw = data.get("equipos")
    if equipos_raw is None:
        equipos_raw = []
    if not isinstance(equipos_raw, list) or not all(isinstance(e, Mapping) for e in equipos_raw):
        raise DictamenError("La lista de equipos no tiene el formato esperado.")

    general = {k: _texto(data.get(k), k) for k in GENERAL_REQUERIDOS}
    propios = {k: _texto(own.get(k), k) for k in (*OWN_REQUERIDOS, "motivo")}
    motivo_global = _PUNTO_FINAL.sub("", propios["motivo"])

    faltantes: list[str] = [k for k in GENERAL_REQUERIDOS if not general[k]]
    problemas: list[str] = []
    if not equipos_raw:
        faltantes.append("equipos")

    equipos: list[dict[str, str]] = []
    falta_motivo = False
    for n, eq in enumerate(equipos_raw, start=1):
        valores = {k: _texto(eq.get(k), k) for k in (*EQUIPO_REQUERIDOS, "numero_inventario", "motivo")}
        faltantes += [f"equipo_{n}_{k}" for k in EQUIPO_REQUERIDOS if not valores[k]]
        valores["motivo"] = _PUNTO_FINAL.sub("", valores["motivo"]) or motivo_global
        falta_motivo = falta_motivo or not valores["motivo"]
        equipos.append(valores)

    faltantes += [k for k in OWN_REQUERIDOS if not propios[k]]
    if falta_motivo:
        faltantes.append("motivo")
    inicial = propios["dictamen_inicial"]
    if inicial and not _SOLO_DIGITOS.fullmatch(inicial):
        problemas.append(
            f"El número de dictamen inicial debe tener solo dígitos (por ejemplo 191); recibí «{inicial}»."
        )
        if "dictamen_inicial" not in faltantes:
            faltantes.append("dictamen_inicial")

    if faltantes or problemas:
        mensajes = list(problemas)
        vacios = [f for f in faltantes if not (f == "dictamen_inicial" and problemas)]
        if vacios:
            mensajes.insert(0, "Faltan datos para generar el dictamen: " + ", ".join(_etiqueta(f) for f in vacios) + ".")
        raise DictamenError(" ".join(mensajes), faltantes)

    return {
        "numero_oficio": _u(propios["numero_oficio"]),
        "fecha_larga": _u(fecha_larga(today if today is not None else hoy_mexico())),
        "nombre_remitente": _u(general["nombre_remitente"]),
        "cargo_remitente": _u(general["cargo_remitente"]),
        "oficio_recibido": _u(general["oficio_recibido"]),
        "fecha_oficio_recibido": _u(general["fecha_oficio_recibido"]),
        "frase_equipos": "del siguiente equipo" if len(equipos) == 1 else "de los siguientes equipos",
        "iniciales": propios["iniciales"],
        "otros_componentes": "---",
        "equipos": [
            {
                "num_dictamen": _num_dictamen(inicial, i),
                "tipo_equipo": _u(eq["tipo_equipo"]),
                "marca": _u(eq["marca"]),
                "modelo": _u(eq["modelo"]),
                "numero_serie": _u(eq["numero_serie"]),
                "numero_inventario": _u(eq["numero_inventario"] or "S/N"),
                "motivo": _u(eq["motivo"]),
            }
            for i, eq in enumerate(equipos)
        ],
    }


def _etiqueta(nombre: str) -> str:
    m = re.fullmatch(r"equipo_(\d+)_(\w+)", nombre)
    if m:
        return f"{ETIQUETAS.get(m.group(2), m.group(2))} del equipo {m.group(1)}"
    return ETIQUETAS.get(nombre, nombre)


# ---------------------------------------------------------------------------
# Render


def render_dictamen(
    data: Mapping[str, Any],
    own: Mapping[str, Any],
    template_path: str | Path,
    today: date | datetime | None = None,
) -> bytes:
    """Genera el DOCX y devuelve sus bytes. ``today`` por defecto = hoy en America/Mexico_City."""
    context = build_context(data, own, today if today is not None else hoy_mexico())
    path = Path(template_path)
    if not path.is_file():
        raise PlantillaError(f"No encontré la plantilla del dictamen en {path}.")

    from docxtpl import DocxTemplate
    from jinja2 import Environment, StrictUndefined, TemplateError

    env = Environment(autoescape=True, undefined=StrictUndefined)
    try:
        template = DocxTemplate(str(path))
        template.render(context, jinja_env=env, autoescape=True)
        buffer = io.BytesIO()
        template.save(buffer)
    except TemplateError as exc:
        raise PlantillaError(f"La plantilla del dictamen tiene un error ({exc}).") from exc
    except Exception as exc:  # zip o XML corruptos
        raise PlantillaError(f"No pude generar el documento con la plantilla {path.name}: {exc}") from exc
    return buffer.getvalue()
