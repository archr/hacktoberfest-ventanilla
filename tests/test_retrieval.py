import numpy as np
import pytest

from app.rag.retriever import Retriever
from app.rag.store import IndexStore, fts_query

DOCS = [
    ("Artículo 76", "LFT, Artículo 76 — CAPITULO IV (Vacaciones)", "Las personas trabajadoras disfrutarán de doce días de vacaciones pagadas.", [1, 0, 0]),
    ("Artículo 48", "LFT, Artículo 48", "El trabajador podrá solicitar la indemnización de tres meses de salario por despido injustificado.", [0, 1, 0]),
    ("Ficha 1/CFF", "Anexo 2, ficha 1/CFF: Solicitud de inscripción en el RFC de personas físicas.", "Acude a la oficina del SAT con identificación oficial.", [0, 0, 1]),
]


class FakeEmbedder:
    def __init__(self, vectors):
        self.vectors = vectors

    async def embed_queries(self, texts):
        return np.array([self.vectors.get(t, [0.0, 0.0, 0.0]) for t in texts], dtype=np.float32)


@pytest.fixture
def store(tmp_path):
    st = IndexStore(tmp_path / "idx.sqlite")
    chunks = [{"section": s, "heading": h, "text": t, "page": i + 1} for i, (s, h, t, _) in enumerate(DOCS)]
    emb = np.array([v for *_, v in DOCS], dtype=np.float32)
    st.replace_document(
        {"id": "lft", "title": "Ley Federal del Trabajo", "short_title": "LFT", "institution": "Diputados",
         "url": "https://example.gob.mx/LFT.pdf", "version": "DOF 2026", "fetched_at": "now", "sha256": "x"},
        chunks,
        emb,
    )
    yield st
    st.close()


def test_fts_query_folds_accents_and_drops_stopwords():
    assert fts_query("¿Cómo me inscribo en el RFC?") == '"inscri"* OR "rfc"'
    assert fts_query("indemnización por despido") == '"indemn"* OR "despid"*'
    assert fts_query("¿y yo?") is None


async def test_bm25_prefix_matches_word_variants(store):
    r = Retriever(store, embedder=None)
    res = await r.search("indemnización", k=1)  # el texto dice "indemnización"; "indemnice" también coincidiría
    assert res[0].section == "Artículo 48"


async def test_bm25_is_accent_insensitive(store):
    r = Retriever(store, embedder=None)
    res = await r.search("inscripcion rfc", k=2)
    assert res[0].section == "Ficha 1/CFF"
    assert res[0].link == "https://example.gob.mx/LFT.pdf#page=3"


async def test_vectors_find_colloquial_question(store):
    # sin coincidencias léxicas: solo el vector puede encontrarlo
    r = Retriever(store, FakeEmbedder({"me corrieron": [0, 1, 0]}))
    res = await r.search("me corrieron", k=1)
    assert res[0].section == "Artículo 48"


async def test_multiple_queries_are_fused(store):
    r = Retriever(store, FakeEmbedder({}))
    res = await r.search(["me corrieron", "despido injustificado indemnización"], k=1)
    assert res[0].section == "Artículo 48"


async def test_replace_document_is_idempotent(store):
    before = store.stats()["chunks"]
    store.replace_document(
        {"id": "lft", "title": "LFT", "short_title": "LFT", "institution": None, "url": None,
         "version": None, "fetched_at": None, "sha256": None},
        [{"section": "Artículo 1", "heading": "h", "text": "texto nuevo", "page": None}],
        None,
    )
    assert before == 3 and store.stats()["chunks"] == 1
    assert store.bm25("vacaciones", 5) == []
