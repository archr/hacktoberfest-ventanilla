"""Técnicos: port de tecnicos.js con correcciones documentadas.

GOLDEN_JS se grabó ejecutando tecnicos.js (dictamen-bot) con TECNICOS_PATH apuntando a cada
catálogo. La prueba diferencial completa contra el JS real se activa con
``DICTAMEN_TECNICOS_JS=/ruta/al/harness`` (directorio con tecnicos.js y node en el PATH).
"""

import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from app.dictamen import tecnicos as T

ROOT = Path(__file__).resolve().parent.parent

ACTUAL = [{"nombre": "", "iniciales": "igf"}, {"nombre": "", "iniciales": "dsa"},
          {"nombre": "", "iniciales": "lafg"}]
NOMBRES = [{"nombre": "Iván García Flores", "iniciales": "igf"},
           {"nombre": "Diana Sánchez Aquino", "iniciales": "dsa"},
           {"nombre": "Luis Alberto Fernández Gómez", "iniciales": "lafg"},
           {"nombre": "Daniel Ortiz", "iniciales": "do"}]

# (texto, resultado de tecnicos.js con ACTUAL, resultado con NOMBRES)
GOLDEN_JS = [
    ("igf", {"valor": "igf"}, {"valor": "igf"}),
    ("IGF", {"valor": "igf"}, {"valor": "igf"}),
    ("igf/dsa", {"valor": "igf/dsa"}, {"valor": "igf/dsa"}),
    ("igf, dsa; lafg", {"valor": "igf/dsa/lafg"}, {"valor": "igf/dsa/lafg"}),
    ("igf\ndsa", {"valor": "igf/dsa"}, {"valor": "igf/dsa"}),
    ("igf y dsa", {"valor": "igf/dsa"}, {"valor": "igf/dsa"}),
    ("igf Y dsa", {"valor": "igf/dsa"}, {"valor": "igf/dsa"}),
    ("Ígf", {"valor": "igf"}, {"valor": "igf"}),
    ("igf dsa", {"valor": "igf/dsa"}, {"valor": "igf/dsa"}),
    ("pedro pérez", {"valor": "pp"}, {"valor": "pp"}),
    ("José", {"valor": "jose"}, {"valor": "jose"}),
    ("ñoño", {"valor": "nono"}, {"valor": "nono"}),
    ("123", {"error": "No pude leer las iniciales."}, {"error": "No pude leer las iniciales."}),
    ("", {"error": "No pude leer las iniciales."}, {"error": "No pude leer las iniciales."}),
    ("  /  ", {"error": "No pude leer las iniciales."}, {"error": "No pude leer las iniciales."}),
    ("igf/igf", {"valor": "igf/igf"}, {"valor": "igf/igf"}),
    ("i.g.f.", {"valor": "igf"}, {"valor": "igf"}),
    ("I. G. F.", {"valor": "igf"}, {"valor": "igf"}),
    ("MDTG/igf", {"valor": "mdtg/igf"}, {"valor": "mdtg/igf"}),
    ("mdtg igf", {"valor": "mi"}, {"valor": "mi"}),
    ("igf-dsa", {"valor": "igfdsa"}, {"valor": "igfdsa"}),
    ("igf & dsa", {"valor": "i&d"}, {"valor": "i&d"}),
    ("igf.dsa", {"valor": "igfdsa"}, {"valor": "igfdsa"}),
    ("D. Sánchez", {"valor": "ds"}, {"valor": "ds"}),
    ("Iván", {"valor": "ivan"}, {"valor": "igf"}),
    ("ivan garcia", {"valor": "ig"}, {"valor": "igf"}),
    ("Diana", {"valor": "diana"}, {"valor": "dsa"}),
    ("d", {"valor": "d"},
     {"error": '"d" coincide con varios técnicos (Diana Sánchez Aquino, Daniel Ortiz). Sé más específico.'}),
    ("Daniel", {"valor": "daniel"}, {"valor": "do"}),
    ("luis alberto", {"valor": "la"}, {"valor": "lafg"}),
    ("lafg do", {"valor": "ld"}, {"valor": "lafg/do"}),
    ("x9z", {"valor": "xz"}, {"valor": "xz"}),
    ("igf dsa", {"valor": "igf/dsa"}, {"valor": "igf/dsa"}),
    ("﻿igf", {"valor": "igf"}, {"valor": "igf"}),
    ("igf y", {"valor": "iy"}, {"valor": "iy"}),
    ("y dsa", {"valor": "yd"}, {"valor": "yd"}),
    ("dsa y igf y lafg", {"valor": "dsa/igf/lafg"}, {"valor": "dsa/igf/lafg"}),
]


@pytest.mark.parametrize("texto, js_actual, js_nombres", GOLDEN_JS)
def test_without_corrections_matches_tecnicos_js(texto, js_actual, js_nombres):
    assert T._resolver(texto, ACTUAL) == js_actual
    assert T._resolver(texto, NOMBRES) == js_nombres


@pytest.mark.parametrize("texto, esperado", [
    ("igf", "igf"), ("IGF/DSA", "igf/dsa"), ("igf y dsa", "igf/dsa"),
    # 1) prefijo MDTG ignorado (la plantilla ya dice MDTG/{iniciales})
    ("MDTG/igf", "igf"), ("mdtg/igf/dsa", "igf/dsa"), ("MDTG igf", "igf"), ("M.D.T.G./dsa", "dsa"),
    ("igf/MDTG", "igf/mdtg"),  # solo como prefijo
    # 2) separadores - & .
    ("igf-dsa", "igf/dsa"), ("igf & dsa", "igf/dsa"), ("igf.dsa", "igf/dsa"), ("igf. dsa.", "igf/dsa"),
    ("i.g.f.", "igf"), ("I. G. F.", "igf"), ("i.g.f./d.s.a.", "igf/dsa"),
    # con el catálogo actual (sin nombres) se siguen aceptando iniciales libres, como hoy
    ("pedro pérez", "pp"), ("José", "jose"), ("xyz", "xyz"),
])
def test_corrections_with_current_catalog(texto, esperado):
    assert T.resolver_iniciales(texto, ACTUAL) == {"valor": esperado}


def test_only_prefix_is_rejected_with_explanation():
    for texto in ("MDTG", "mdtg/", " MDTG ; "):
        r = T.resolver_iniciales(texto, ACTUAL)
        assert "error" in r and "MDTG/" in r["error"] and "plantilla" in r["error"]
    assert T.resolver_iniciales("", ACTUAL) == {"error": "No pude leer las iniciales."}
    assert T.resolver_iniciales(None, ACTUAL) == {"error": "No pude leer las iniciales."}


def test_prefix_is_configurable():
    assert T.resolver_iniciales("ABC/igf", ACTUAL, prefijo="ABC") == {"valor": "igf"}
    assert T.resolver_iniciales("MDTG/igf", ACTUAL, prefijo="ABC") == {"valor": "mdtg/igf"}


@pytest.mark.parametrize("texto, esperado", [
    ("Iván", "igf"), ("ivan garcia", "igf"), ("Diana", "dsa"), ("Daniel", "do"),
    ("luis alberto", "lafg"), ("igf dsa", "igf/dsa"), ("MDTG/Diana", "dsa"),
    ("Iván-Diana", "igf/dsa"), ("́/igf", "igf"),
])
def test_named_catalog_resolves_known(texto, esperado):
    assert T.resolver_iniciales(texto, NOMBRES) == {"valor": esperado}


@pytest.mark.parametrize("texto", ["Pedro Pérez", "xyz", "José", "123", "igf/xyz"])
def test_named_catalog_rejects_unknown_listing_valid(texto):
    r = T.resolver_iniciales(texto, NOMBRES)
    assert "valor" not in r
    assert "No reconozco" in r["error"]
    assert "Iván García Flores (igf)" in r["error"] and "Daniel Ortiz (do)" in r["error"]


def test_named_catalog_ambiguous_keeps_js_message():
    r = T.resolver_iniciales("D. Sánchez", NOMBRES)  # "D" coincide con Diana y Daniel
    assert r == {"error": '"D" coincide con varios técnicos (Diana Sánchez Aquino, Daniel Ortiz). '
                          "Sé más específico."}


def test_mixed_catalog_lists_initials_without_name():
    mixto = [{"nombre": "Iván García Flores", "iniciales": "igf"}, {"nombre": "", "iniciales": "dsa"}]
    assert T.resolver_iniciales("dsa", mixto) == {"valor": "dsa"}
    r = T.resolver_iniciales("Pedro", mixto)
    assert r["error"].endswith("Técnicos válidos: Iván García Flores (igf), dsa.")


def test_initial_of_astral_character_is_whole():
    # JS tomaba media unidad UTF-16 (sustituto suelto, inválido en XML); aquí el carácter completo
    r = T.resolver_iniciales("😀x yz", [])
    assert r == {"valor": "😀y"}
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in r["valor"])


def test_cargar_tecnicos_real_file():
    tecnicos = T.cargar_tecnicos(ROOT / "config" / "dictamen" / "tecnicos.json")
    assert tecnicos == ACTUAL
    assert T.resolver_iniciales("igf/dsa", tecnicos) == {"valor": "igf/dsa"}


def test_cargar_tecnicos_tolerates_bad_files(tmp_path, caplog):
    missing = tmp_path / "no_existe.json"
    assert T.cargar_tecnicos(missing) == []
    assert "No existe el catálogo" in caplog.text
    bad = tmp_path / "malo.json"
    bad.write_text("{no es json", encoding="utf-8")
    assert T.cargar_tecnicos(bad) == []
    bad.write_text('{"iniciales": "igf"}', encoding="utf-8")
    assert T.cargar_tecnicos(bad) == []


def test_cargar_tecnicos_sanitizes_entries(tmp_path):
    f = tmp_path / "t.json"
    f.write_text("﻿" + json.dumps([
        {"nombre": " Ana López ", "iniciales": " AL "}, {"nombre": None, "iniciales": "dsa"},
        {"nombre": "Sin iniciales"}, {"iniciales": ""}, {"iniciales": 5}, "texto", None,
    ]), encoding="utf-8")
    assert T.cargar_tecnicos(f) == [{"nombre": "Ana López", "iniciales": "AL"},
                                    {"nombre": "", "iniciales": "dsa"}]


def test_etiqueta_tecnico():
    assert T.etiqueta_tecnico({"nombre": "Ana", "iniciales": "al"}) == "Ana (al)"
    assert T.etiqueta_tecnico({"nombre": "", "iniciales": "al"}) == "al"


# ----------------------------------------------------------------- diferencial contra el JS real

JS_RUNNER = r"""
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';
const { resolverIniciales } = await import(pathToFileURL(path.join(process.env.HARNESS, 'tecnicos.js')).href);
const casos = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = [];
for (const c of casos) {
  fs.writeFileSync(process.env.TECNICOS_PATH, JSON.stringify(c.catalog));
  out.push(c.texts.map(t => { try { return resolverIniciales(t); } catch (e) { return { crash: String(e) }; } }));
}
process.stdout.write(JSON.stringify(out));
"""

PIECES = ["igf", "IGF", "dsa", "lafg", "do", "Iván", "García", "Diana", "Sánchez", "d", "g", "MDTG",
          "mdtg", "M.D.T.G.", "pedro pérez", "123", "ñoño", "José", "i.g.f.", "I. G. F.", "D.", "&", "-",
          ".", "y", "Y", "́", "Straße", "İ", "a b", "﻿igf", " dsa", "\x1cigf",
          "\u0085dsa", "ΣΑΣ", "igf dsa", "luis alberto", "(igf)", "igf.", "MDTG igf", ""]
SEPS = ["/", ",", ";", "\n", " y ", " Y ", " ", "-", "&", ".", " - ", "\t", "\r\n", ", ", " & ",
        " y ", "\u0085", "\x1c"]


@pytest.mark.skipif(not os.environ.get("DICTAMEN_TECNICOS_JS") or not shutil.which("node"),
                    reason="define DICTAMEN_TECNICOS_JS con la ruta al harness de dictamen-bot")
def test_differential_against_tecnicos_js(tmp_path):
    rng = random.Random(1031)
    texts = list(dict.fromkeys(
        [t for t, _, _ in GOLDEN_JS]
        + ["".join(rng.choice(PIECES) + rng.choice(SEPS) for _ in range(rng.randint(1, 4)))
           for _ in range(1500)]
    ))
    catalogs = [ACTUAL, NOMBRES, [], [{"nombre": "Iván García Flores", "iniciales": "igf"},
                                      {"nombre": "", "iniciales": "DSA"}]]
    runner = tmp_path / "runner.mjs"
    runner.write_text(JS_RUNNER)
    proc = subprocess.run(
        ["node", str(runner)], input=json.dumps([{"catalog": c, "texts": texts} for c in catalogs]),
        capture_output=True, text=True, check=True, timeout=120,
        env={**os.environ, "HARNESS": os.environ["DICTAMEN_TECNICOS_JS"],
             "TECNICOS_PATH": str(tmp_path / "tecnicos.json")},
    )
    flags = {"prefijo": {"prefijo": "MDTG"}, "separadores": {"separadores_extra": True},
             "estricto": {"estricto": True}}
    for catalog, js_results in zip(catalogs, json.loads(proc.stdout)):
        named = any(t["nombre"] for t in catalog)
        for texto, js in zip(texts, js_results):
            assert "crash" not in js
            assert T._resolver(texto, catalog) == js, texto  # sin correcciones: idéntico
            if T.resolver_iniciales(texto, catalog) == js:
                continue
            triggered = [name for name, kw in flags.items() if T._resolver(texto, catalog, **kw) != js]
            for name in triggered:  # cada corrección solo actúa cuando aplica
                if name == "separadores":
                    assert any(c in texto for c in "-&."), texto
                elif name == "estricto":
                    assert named, texto
                else:
                    tokens = T._tokens(texto, True) or T._tokens(texto, False)
                    assert tokens and T._norm(tokens[0]).split()[0] == "mdtg", texto
