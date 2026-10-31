"""Búsqueda híbrida: BM25 (FTS5) + similitud coseno, fusionadas con RRF."""

from __future__ import annotations

import asyncio
import logging

import numpy as np

from ..llm import Embedder
from .store import ChunkRecord, IndexStore

log = logging.getLogger(__name__)


class Retriever:
    def __init__(
        self,
        store: IndexStore,
        embedder: Embedder | None,
        candidates: int = 40,
        max_per_section: int = 2,
        rrf_k: int = 60,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.candidates = candidates
        self.max_per_section = max_per_section
        self.rrf_k = rrf_k
        self.reload()

    def reload(self) -> None:
        self._ids, self._matrix = self.store.load_embeddings()
        log.info("Índice cargado: %s", self.store.stats())

    @property
    def has_vectors(self) -> bool:
        return self._matrix.size > 0

    async def search(self, question: str | list[str], k: int = 6) -> list[ChunkRecord]:
        """Busca con una o varias consultas (p. ej. original + reescrita) y fusiona con RRF."""
        queries = [q for q in ([question] if isinstance(question, str) else question) if q and q.strip()]
        if not queries:
            return []
        rankings: list[list[int]] = []
        for q in queries:
            rankings.append(await asyncio.to_thread(self.store.bm25, q, self.candidates))
        if self.embedder is not None and self.has_vectors:
            try:
                qvecs = await self.embedder.embed_queries(queries)
                rankings.extend(self._vector_search(v, self.candidates) for v in qvecs)
            except Exception as exc:  # noqa: BLE001 - degradar a solo BM25
                log.warning("Embeddings no disponibles, uso solo BM25: %s", exc)

        scores: dict[int, float] = {}
        for ranking in rankings:
            for rank, cid in enumerate(ranking):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (self.rrf_k + rank + 1)
        ordered = sorted(scores, key=scores.get, reverse=True)
        records = await asyncio.to_thread(self.store.get_chunks, ordered[: self.candidates])

        picked: list[ChunkRecord] = []
        per_section: dict[tuple[str, str], int] = {}
        for cid in ordered:
            rec = records.get(cid)
            if rec is None:
                continue
            key = (rec.doc_id, rec.section)
            if per_section.get(key, 0) >= self.max_per_section:
                continue
            per_section[key] = per_section.get(key, 0) + 1
            picked.append(rec)
            if len(picked) >= k:
                break
        return picked

    def _vector_search(self, qvec: np.ndarray, limit: int) -> list[int]:
        if qvec.shape[0] != self._matrix.shape[1]:
            raise ValueError("Dimensión de embedding distinta a la del índice; reindexa.")
        sims = self._matrix @ qvec
        limit = min(limit, sims.shape[0])
        top = np.argpartition(-sims, limit - 1)[:limit]
        top = top[np.argsort(-sims[top])]
        return [int(self._ids[i]) for i in top]
