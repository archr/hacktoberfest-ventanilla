import pytest

from app.dictamen import schema as S


def equipo(**kw):
    base = {"tipo_equipo": "MONITOR", "marca": "DELL", "modelo": "P2219H", "numero_serie": "CN0KW3",
            "numero_inventario": None, "motivo": None}
    return {**base, **kw}


def completo():
    data = {"oficio_recibido": "FGEO/DA/0532/2026", "fecha_oficio_recibido": "20 DE AGOSTO DEL 2026",
            "cargo_remitente": "DIRECTORA ADMINISTRATIVA", "nombre_remitente": "MTRA. LAURA MENDOZA",
            "equipos": [equipo()]}
    own = {"numero_oficio": "FGEO/CGSI/1429/2026", "dictamen_inicial": "191", "iniciales": "igf",
           "motivo": "EQUIPO CON FALLA DE ENCENDIDO"}
    return data, own


def test_field_lists_follow_the_contract():
    assert [f["name"] for f in S.GENERAL_FIELDS] == [
        "oficio_recibido", "fecha_oficio_recibido", "cargo_remitente", "nombre_remitente"]
    assert [f["name"] for f in S.EQUIPO_FIELDS] == [
        "tipo_equipo", "marca", "modelo", "numero_serie", "numero_inventario", "motivo"]
    assert [f["name"] for f in S.OWN_FIELDS] == ["numero_oficio", "dictamen_inicial", "iniciales", "motivo"]
    required = {f["name"]: f["required"] for f in S.EQUIPO_FIELDS}
    assert required["numero_inventario"] is False and required["motivo"] is False
    assert all(required[k] for k in ("tipo_equipo", "marca", "modelo", "numero_serie"))
    for f in S.GENERAL_FIELDS + S.EQUIPO_FIELDS + S.OWN_FIELDS:
        assert {"name", "label", "required", "example"} <= set(f)
        assert isinstance(f["required"], bool) and f["label"] and f["example"]
    own_examples = {f["name"]: f["example"] for f in S.OWN_FIELDS}
    assert own_examples == {"numero_oficio": "FGEO/CGSI/1429/2025", "dictamen_inicial": "191",
                            "iniciales": "igf/dsa", "motivo": "EQUIPO CON FALLA DE ENCENDIDO"}
    assert S.MAX_EQUIPOS == 100


def test_field_spec_returns_independent_copies():
    spec = S.field_spec()
    assert set(spec) == {"general", "equipo", "own"}
    spec["general"][0]["label"] = "otra cosa"
    assert S.field_spec()["general"][0]["label"] == "Número del oficio recibido"


def test_empty_draft():
    draft = S.empty_draft()
    assert draft == {
        "data": {"oficio_recibido": None, "fecha_oficio_recibido": None, "cargo_remitente": None,
                 "nombre_remitente": None, "equipos": []},
        "own": {"numero_oficio": None, "dictamen_inicial": None, "iniciales": None, "motivo": None},
    }
    draft["data"]["equipos"].append(1)
    assert S.empty_draft()["data"]["equipos"] == []


def test_normalize_trims_and_empties_and_fixes_shape():
    raw = {
        "oficio_recibido": "  FGEO/DA/0532/2026 \n",
        "fecha_oficio_recibido": "2026-08-20",
        "cargo_remitente": "",
        "nombre_remitente": "MTRA.\tLAURA   ISABEL\nMENDOZA",
        "extra": "se ignora",
        "equipos": [
            {"tipo_equipo": " MONITOR ", "marca": "DELL", "modelo": 2219, "numero_serie": "CN0\x0bKW3",
             "numero_inventario": "", "inventado": "x"},
            {"tipo_equipo": None, "marca": "  ", "modelo": None, "numero_serie": None,
             "numero_inventario": None},  # fila vacía: se descarta
            "texto suelto", None, 7,
            {"tipo_equipo": "CPU", "numero_inventario": "null", "numero_serie": "---", "marca": True,
             "modelo": ["x"], "motivo": "  NO ENCIENDE  "},
        ],
    }
    data = S.normalize_extraction(raw)
    assert list(data) == ["oficio_recibido", "fecha_oficio_recibido", "cargo_remitente",
                          "nombre_remitente", "equipos"]
    assert data["oficio_recibido"] == "FGEO/DA/0532/2026"
    assert data["fecha_oficio_recibido"] == "20 DE AGOSTO DEL 2026"
    assert data["cargo_remitente"] is None
    assert data["nombre_remitente"] == "MTRA. LAURA ISABEL MENDOZA"
    assert data["equipos"] == [
        {"tipo_equipo": "MONITOR", "marca": "DELL", "modelo": "2219", "numero_serie": "CN0KW3",
         "numero_inventario": None, "motivo": None},
        {"tipo_equipo": "CPU", "marca": None, "modelo": None, "numero_serie": None,
         "numero_inventario": None, "motivo": "NO ENCIENDE"},
    ]


@pytest.mark.parametrize("raw", [None, [], "texto", 5, {"equipos": None}, {"equipos": "3"},
                                 {"equipos": {"tipo_equipo": "CPU"}}])
def test_normalize_tolerates_garbage(raw):
    data = S.normalize_extraction(raw)
    assert data["equipos"] == [] and data["oficio_recibido"] is None


def test_normalize_caps_equipos_and_length():
    raw = {"oficio_recibido": "X" * 900,
           "equipos": [{"tipo_equipo": f"CPU {i}"} for i in range(S.MAX_EQUIPOS + 20)]}
    data = S.normalize_extraction(raw)
    assert len(data["equipos"]) == S.MAX_EQUIPOS
    assert len(data["oficio_recibido"]) == S.MAX_FIELD_LEN


def test_clean_text_removes_xml_invalid_characters():
    assert S.clean_text("A\x00B\x08C\x0cD\x1fE￾F\ud800G") == "ABCDEFG"
    assert S.clean_text("Ñandú «Pérez» — 5°") == "Ñandú «Pérez» — 5°"
    assert S.clean_text(12.0) == "12" and S.clean_text(12.5) == "12.5"
    assert S.clean_text(float("nan")) is None and S.clean_text(False) is None
    for empty in ["", "   ", "null", "None", "N/A", "-", "—", "..."]:
        assert S.clean_text(empty) is None


@pytest.mark.parametrize("value, expected", [
    ("2026-08-20", "20 DE AGOSTO DEL 2026"),
    ("2026-8-5", "05 DE AGOSTO DEL 2026"),
    ("2026/12/31", "31 DE DICIEMBRE DEL 2026"),
    ("2026-08-20T10:30:00Z", "20 DE AGOSTO DEL 2026"),
    ("20/08/2026", "20 DE AGOSTO DEL 2026"),
    ("5-10-2026", "05 DE OCTUBRE DEL 2026"),
    ("3 de septiembre de 2026", "03 DE SEPTIEMBRE DEL 2026"),
    ("3 DE SEPTIEMBRE DE 2026", "03 DE SEPTIEMBRE DEL 2026"),
    ("1º de octubre del 2026", "01 DE OCTUBRE DEL 2026"),
    ("15 de setiembre de 2026", "15 DE SEPTIEMBRE DEL 2026"),
    ("20 DE AGOSTO DEL 2026", "20 DE AGOSTO DEL 2026"),
    # imposibles u otros formatos: no se tocan (la persona los revisa en la tarjeta)
    ("2026-02-30", "2026-02-30"),
    ("08/13/2026", "08/13/2026"),
    ("31 DE FEBRERO DEL 2026", "31 DE FEBRERO DEL 2026"),
    ("miércoles 3 de septiembre de 2026", "miércoles 3 de septiembre de 2026"),
    ("AGOSTO 2026", "AGOSTO 2026"),
    (None, None),
])
def test_normalize_fecha(value, expected):
    assert S.normalize_fecha(value) == expected


def test_faltantes_complete_is_empty():
    data, own = completo()
    assert S.faltantes(data, own) == []


def test_faltantes_uses_bot_names_in_order():
    data = {"oficio_recibido": "X", "fecha_oficio_recibido": None, "cargo_remitente": "  ",
            "nombre_remitente": "Y",
            "equipos": [equipo(), equipo(marca=None, numero_serie=""), equipo(tipo_equipo=None)]}
    assert S.faltantes(data, {}) == [
        "fecha_oficio_recibido", "cargo_remitente", "equipo_2_marca", "equipo_2_numero_serie",
        "equipo_3_tipo_equipo", "numero_oficio", "dictamen_inicial", "iniciales", "motivo"]


def test_faltantes_requires_at_least_one_equipo():
    data, own = completo()
    data["equipos"] = []
    assert S.faltantes(data, own) == ["equipos"]
    assert "equipos" in S.faltantes(S.empty_draft()["data"], S.empty_draft()["own"])


@pytest.mark.parametrize("value, ok", [
    ("191", True), ("0191", True), (" 191 ", True), (191, True), ("0", True),
    ("abc", False), ("DT-191", False), ("19.1", False), ("-5", False), ("", False), (None, False),
    ("١٢٣", False), (True, False), (-1, False),
])
def test_faltantes_validates_dictamen_inicial(value, ok):
    data, own = completo()
    own["dictamen_inicial"] = value
    assert ("dictamen_inicial" not in S.faltantes(data, own)) is ok


def test_motivo_only_required_when_some_equipo_lacks_it():
    data, own = completo()
    own["motivo"] = None
    assert S.faltantes(data, own) == ["motivo"]
    data["equipos"] = [equipo(motivo="NO ENCIENDE"), equipo(motivo="PANTALLA ROTA")]
    assert S.faltantes(data, own) == []
    data["equipos"].append(equipo())
    assert S.faltantes(data, own) == ["motivo"]


def test_inventario_is_optional():
    data, own = completo()
    data["equipos"] = [equipo(numero_inventario=None)]
    assert S.faltantes(data, own) == []


@pytest.mark.parametrize("data, own", [(None, None), ({"equipos": "3"}, "x"), ({"equipos": [None, 5]}, {})])
def test_faltantes_tolerates_garbage(data, own):
    result = S.faltantes(data, own)
    assert "equipos" in result and "numero_oficio" in result


def test_normalize_is_idempotent():
    # la API vuelve a normalizar lo que envía la tarjeta: aplicar dos veces no debe cambiar nada
    raw = {"oficio_recibido": " A\x0bB ", "fecha_oficio_recibido": "3 de septiembre de 2026",
           "nombre_remitente": "N/A", "equipos": [{"tipo_equipo": "CPU", "numero_inventario": "-",
                                                    "motivo": " NO ENCIENDE. "}, {}]}
    once = S.normalize_extraction(raw)
    assert S.normalize_extraction(once) == once
    assert once["fecha_oficio_recibido"] == "03 DE SEPTIEMBRE DEL 2026"
    assert once["equipos"] == [{"tipo_equipo": "CPU", "marca": None, "modelo": None, "numero_serie": None,
                                "numero_inventario": None, "motivo": "NO ENCIENDE."}]
