"""Índice en SQLite: texto (FTS5/BM25, sin acentos) + embeddings (float32)."""

from __future__ import annotations

import re
import sqlite3
import threading
from functools import wraps
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    short_title TEXT NOT NULL,
    institution TEXT,
    url TEXT,
    version TEXT,
    fetched_at TEXT,
    sha256 TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    section TEXT NOT NULL,
    heading TEXT NOT NULL,
    text TEXT NOT NULL,
    page INTEGER,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    heading, text, content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

# Palabras vacías frecuentes en preguntas ciudadanas (sin acentos, minúsculas).
STOPWORDS = set(
    """
    a al algo algun alguna algunas alguno algunos ante antes aqui asi aun como con contra cual cuales
    cuando cuanto cuantos de del desde donde dos e el ella ellas ellos en entre era es esa esas ese
    eso esos esta estan estas este esto estos fue ha hace hacer hacerlo han hay hasta la las le les lo
    los mas me mi mis mucho muy necesito ni no nos o os otra otro para pero poco por porque puede
    puedo que quien se segun ser si sin sobre solo son su sus tambien tan tengo tiene todo tu tus un
    una uno unos y ya yo quiero saber debo hago tramite tramites favor ayuda ayudame cual
    """.split()
)


@dataclass
class ChunkRecord:
    id: int
    doc_id: str
    section: str
    heading: str
    text: str
    page: int | None
    doc_title: str
    short_title: str
    institution: str | None
    url: str | None
    version: str | None

    @property
    def link(self) -> str | None:
        if not self.url:
            return None
        if self.page and self.url.lower().split("?")[0].endswith(".pdf"):
            return f"{self.url}#page={self.page}"
        return self.url


def _fold(text: str) -> str:
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


STEM_MIN_LEN = 7  # palabras más largas se buscan por prefijo (pseudo-stemming en español)
STEM_PREFIX = 6  # "indemnización"/"indemnice" -> indemn*, "inscripción"/"inscribir" -> inscri*


def fts_query(question: str) -> str | None:
    tokens = re.findall(r"\w+", _fold(question))
    terms: list[str] = []
    for t in tokens:
        if t in STOPWORDS or (len(t) < 3 and not t.isdigit()):
            continue
        term = f'"{t[:STEM_PREFIX]}"*' if len(t) >= STEM_MIN_LEN and t.isalpha() else f'"{t}"'
        if term not in terms:
            terms.append(term)
    if not terms:
        return None
    return " OR ".join(terms[:24])


def _synchronized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


class IndexStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    @_synchronized
    def close(self) -> None:
        self.conn.close()

    # --- escritura (ingesta) ---
    @_synchronized
    def replace_document(
        self,
        doc: dict,
        chunks: list[dict],
        embeddings: np.ndarray | None,
    ) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc["id"],))
            self.conn.execute(
                "INSERT OR REPLACE INTO documents(id, title, short_title, institution, url, version,"
                " fetched_at, sha256) VALUES (:id, :title, :short_title, :institution, :url, :version,"
                " :fetched_at, :sha256)",
                doc,
            )
            for i, ch in enumerate(chunks):
                emb = None
                if embeddings is not None and len(embeddings):
                    emb = np.asarray(embeddings[i], dtype=np.float32).tobytes()
                self.conn.execute(
                    "INSERT INTO chunks(doc_id, ord, section, heading, text, page, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (doc["id"], i, ch["section"], ch["heading"], ch["text"], ch.get("page"), emb),
                )
            self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    # --- lectura ---
    @_synchronized
    def stats(self) -> dict:
        docs = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        with_emb = self.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        return {"documents": docs, "chunks": chunks, "chunks_with_embeddings": with_emb}

    @_synchronized
    def list_documents(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT d.id, d.title, d.short_title, d.institution, d.url, d.version, d.fetched_at,"
            " COUNT(c.id) AS chunks FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id"
            " GROUP BY d.id ORDER BY d.institution, d.short_title"
        ).fetchall()
        return [dict(r) for r in rows]

    @_synchronized
    def load_embeddings(self) -> tuple[np.ndarray, np.ndarray]:
        rows = self.conn.execute(
            "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL ORDER BY id"
        ).fetchall()
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
        ids = np.array([r["id"] for r in rows], dtype=np.int64)
        matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        return ids, matrix

    @_synchronized
    def bm25(self, question: str, limit: int) -> list[int]:
        q = fts_query(question)
        if not q:
            return []
        rows = self.conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
            " ORDER BY bm25(chunks_fts, 2.0, 1.0) LIMIT ?",
            (q, limit),
        ).fetchall()
        return [r[0] for r in rows]

    @_synchronized
    def get_chunks(self, ids: list[int]) -> dict[int, ChunkRecord]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            "SELECT c.id, c.doc_id, c.section, c.heading, c.text, c.page, d.title AS doc_title,"
            " d.short_title, d.institution, d.url, d.version"
            f" FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})",
            ids,
        ).fetchall()
        return {r["id"]: ChunkRecord(**dict(r)) for r in rows}
