"""Catálogo de técnicos y resolución de iniciales (port de tecnicos.js del bot original).

Conserva la semántica de ``resolverIniciales``/``resolverToken`` (mismos separadores ``/ , ;``
salto de línea y `` y ``; misma normalización: NFD, sin diacríticos U+0300–U+036F, minúsculas,
``trim`` y ``\\s`` de JavaScript), con estas correcciones documentadas:

1. Se ignora un prefijo igual a ``MDTG`` (las iniciales del coordinador ya están en la
   plantilla como ``MDTG/{iniciales}``; antes salía ``MDTG/mdtg/igf``).
2. ``-``, ``&`` y ``.`` también separan técnicos (antes ``igf-dsa`` -> ``igfdsa`` e
   ``igf & dsa`` -> ``i&d``). Una abreviatura de letras sueltas con punto (``i.g.f.``) sigue
   leyéndose como ``igf``, igual que antes.
3. Si el catálogo tiene NOMBRES llenos, un token que no corresponde a ninguna inicial ni a un
   único nombre se rechaza con un error que lista los técnicos válidos (antes se convertía en
   silencio en iniciales inventadas). Con el catálogo actual (nombres vacíos) se aceptan
   iniciales libres como hoy.

Robustez: entradas sin ``iniciales`` se descartan al cargar (en JS tronaban); en modo
estricto los tokens que quedan vacíos tras normalizar (p. ej. solo un acento combinante) se
ignoran (en JS coincidían con cualquier técnico con nombre), y la inicial de una palabra es
su primer carácter completo (JS tomaba media unidad UTF-16 con emojis).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

COORDINADOR_INICIALES = "MDTG"

# Equivalentes exactos de \s y String.prototype.trim de JavaScript (Python incluye otros).
_JS_WS_CHARS = (
    "\t\n\x0b\x0c\r   " + "".join(chr(c) for c in range(0x2000, 0x200B))
    + "    　﻿"
)
_WS = "[" + re.escape(_JS_WS_CHARS) + "]"
_SPLIT_JS = re.compile(r"[/,;\n]+|" + _WS + r"+y" + _WS + "+", re.IGNORECASE)
_SPLIT_EXTRA = re.compile(r"[/,;\n&\-]+|" + _WS + r"+y" + _WS + "+", re.IGNORECASE)
_WORDS = re.compile(_WS + "+")
_MARKS = re.compile("[̀-ͯ]")
_NOT_LETTER = re.compile("[^a-zñ]")
_DOT_ABBR = re.compile(r"(?:[^\W\d_]" + _WS + r"*\." + _WS + r"*)+[^\W\d_]?", re.UNICODE)


def _trim(s: str) -> str:
    return s.strip(_JS_WS_CHARS)


def _norm(s: str) -> str:
    return _trim(_MARKS.sub("", unicodedata.normalize("NFD", s)).lower())


def cargar_tecnicos(path: str | Path) -> list[dict[str, str]]:
    """Lee el catálogo ``[{"nombre", "iniciales"}]``. Si falta o es inválido devuelve ``[]``
    (como el original) pero lo registra en el log."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        log.warning("No existe el catálogo de técnicos %s; se aceptarán iniciales libres", path)
        return []
    except (OSError, ValueError) as exc:
        log.warning("No pude leer el catálogo de técnicos %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        log.warning("El catálogo de técnicos %s debe ser una lista", path)
        return []
    tecnicos = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        iniciales = item.get("iniciales")
        nombre = item.get("nombre")
        if not isinstance(iniciales, str) or not _norm(iniciales):
            continue
        tecnicos.append({
            "nombre": _trim(nombre) if isinstance(nombre, str) else "",
            "iniciales": _trim(iniciales),
        })
    return tecnicos


def etiqueta_tecnico(tecnico: dict[str, str]) -> str:
    """'Ana López (igf)' o solo 'igf' si no tiene nombre."""
    nombre = tecnico.get("nombre") or ""
    return f"{nombre} ({tecnico['iniciales']})" if nombre else tecnico["iniciales"]


def _resolver_token(token: str, tecnicos: list[dict[str, str]], estricto: bool) -> dict[str, str]:
    t = _norm(token)
    for x in tecnicos:
        if _norm(x["iniciales"]) == t:
            return {"valor": x["iniciales"].lower()}

    palabras = _WORDS.split(t)
    coinciden = [
        x for x in tecnicos
        if x["nombre"] and all(
            any(w.startswith(p) for w in _WORDS.split(_norm(x["nombre"]))) for p in palabras
        )
    ]
    if len(coinciden) == 1:
        return {"valor": coinciden[0]["iniciales"].lower()}
    if len(coinciden) > 1:
        nombres = ", ".join(c["nombre"] for c in coinciden)
        return {"error": f'"{token}" coincide con varios técnicos ({nombres}). Sé más específico.'}

    conocidas = {_norm(x["iniciales"]) for x in tecnicos}
    if len(palabras) > 1 and all(p in conocidas for p in palabras):
        return {"valor": "/".join(palabras)}
    if estricto:
        validos = ", ".join(etiqueta_tecnico(x) for x in tecnicos)
        return {"error": f'No reconozco "{token}" como técnico. Técnicos válidos: {validos}.'}
    if len(palabras) == 1:
        return {"valor": _NOT_LETTER.sub("", t)}
    return {"valor": "".join(p[0] for p in palabras)}  # nombre desconocido: sus iniciales


def _tokens(texto: str, separadores_extra: bool) -> list[str]:
    if not separadores_extra:
        return [tk for tk in (_trim(s) for s in _SPLIT_JS.split(texto)) if tk]
    tokens: list[str] = []
    for pieza in _SPLIT_EXTRA.split(texto):
        pieza = _trim(pieza)
        if "." in pieza and _DOT_ABBR.fullmatch(pieza):
            partes = [_WORDS.sub("", pieza.replace(".", ""))]  # i.g.f. -> igf
        else:
            partes = pieza.split(".")
        tokens.extend(tk for tk in (_trim(p) for p in partes) if tk)
    return tokens


def _quitar_prefijo(tokens: list[str], prefijo: str) -> list[str]:
    if not tokens:
        return tokens
    pref = _norm(prefijo)
    primero = _norm(tokens[0])
    if primero == pref:
        return tokens[1:]
    palabras = _WORDS.split(primero)
    if len(palabras) > 1 and palabras[0] == pref:
        resto = _WORDS.split(_trim(tokens[0]), maxsplit=1)[1]
        return [resto, *tokens[1:]]
    return tokens


def _resolver(
    texto: Any,
    tecnicos: list[dict[str, str]],
    *,
    prefijo: str | None = None,
    separadores_extra: bool = False,
    estricto: bool = False,
) -> dict[str, str]:
    """Núcleo configurable. Sin argumentos opcionales reproduce tecnicos.js (pruebas
    diferenciales); ``resolver_iniciales`` activa las tres correcciones."""
    if not isinstance(texto, str):
        return {"error": "No pude leer las iniciales."}
    tokens = _tokens(texto, separadores_extra)
    habia_prefijo = False
    if prefijo:
        sin_prefijo = _quitar_prefijo(tokens, prefijo)
        habia_prefijo = sin_prefijo != tokens
        tokens = sin_prefijo
    estricto = estricto and any(x["nombre"] for x in tecnicos)
    out = []
    for tk in tokens:
        if estricto and not _norm(tk):
            continue  # en JS coincidía con cualquier técnico con nombre
        r = _resolver_token(tk, tecnicos, estricto)
        if "error" in r:
            return r
        if r["valor"]:
            out.append(r["valor"])
    if out:
        return {"valor": "/".join(out)}
    if habia_prefijo:
        return {"error": f'Escribe las iniciales del técnico; "{prefijo}/" ya está en la plantilla.'}
    return {"error": "No pude leer las iniciales."}


def resolver_iniciales(
    texto: str, tecnicos: list[dict[str, str]], *, prefijo: str = COORDINADOR_INICIALES
) -> dict[str, str]:
    """Texto libre -> ``{"valor": "igf/dsa"}`` o ``{"error": "mensaje para la persona"}``.

    Acepta iniciales o nombres (si el catálogo los tiene) separados por ``/ , ; - & .``,
    salto de línea o `` y ``.
    """
    return _resolver(texto, tecnicos, prefijo=prefijo, separadores_extra=True, estricto=True)
