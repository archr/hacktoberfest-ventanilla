"""Descarga, parsea, fragmenta, vectoriza e indexa las fuentes de config/sources.yaml.

Uso:
    python -m app.rag.ingest                 # todas las fuentes
    python -m app.rag.ingest --only cff lft  # solo algunas
    python -m app.rag.ingest --refresh       # vuelve a descargar
    python -m app.rag.ingest --no-embed      # solo BM25 (sin modelo de embeddings)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pymupdf
import yaml

from ..llm import OpenAICompatibleEmbedder
from ..settings import Settings, get_settings
from .parsers import PARSERS, html_to_text
from .store import IndexStore
from .text import clean_pages, split_text, strip_page_breaks

log = logging.getLogger("ingest")
USER_AGENT = "Mozilla/5.0 (compatible; gobchat-mvp/0.1; +https://www.gob.mx)"


@dataclass
class Source:
    id: str
    title: str
    short_title: str
    url: str
    institution: str = ""
    format: str = "pdf"  # pdf | html
    parser: str = "generic"
    version: str | None = None
    max_chars: int = 1800


def load_sources(path: Path) -> list[Source]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Source(**item) for item in data.get("sources", [])]


def download(src: Source, raw_dir: Path, refresh: bool) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{src.id}.{src.format}"
    if target.exists() and not refresh:
        return target
    log.info("Descargando %s", src.url)
    with httpx.Client(
        timeout=httpx.Timeout(180.0, connect=20.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        resp = client.get(src.url)
        resp.raise_for_status()
    tmp = target.with_suffix(".part")
    tmp.write_bytes(resp.content)
    tmp.replace(target)
    return target


def extract(src: Source, path: Path) -> tuple[str, str | None]:
    """Devuelve (texto limpio, versión detectada)."""
    if src.format == "pdf":
        with pymupdf.open(path) as doc:
            pages = [page.get_text() for page in doc]
        version = None
        m = re.search(r"[ÚU]ltima\s+[Rr]eforma\s+(?:publicada\s+)?DOF\s+(\d{2}-\d{2}-\d{4})", "\n".join(pages[:2]))
        if m:
            version = f"Última reforma DOF {m.group(1)}"
        return clean_pages(pages), version
    if src.format == "html":
        _, text = html_to_text(path.read_text(encoding="utf-8", errors="replace"))
        return text, None
    raise ValueError(f"Formato no soportado: {src.format}")


def build_chunks(src: Source, text: str) -> list[dict]:
    parser = PARSERS.get(src.parser)
    if parser is None:
        raise ValueError(f"Parser desconocido '{src.parser}' en {src.id}")
    chunks: list[dict] = []
    for sec in parser(text, src.short_title):
        for piece in split_text(sec.text, sec.page, max_chars=src.max_chars):
            body = strip_page_breaks(piece.text)
            if len(body) < 40:
                continue
            chunks.append(
                {"section": sec.section, "heading": sec.heading, "text": body, "page": piece.page}
            )
    return chunks


def ingest_source(
    src: Source,
    settings: Settings,
    store: IndexStore,
    embedder: OpenAICompatibleEmbedder | None,
    refresh: bool = False,
) -> int:
    t0 = time.time()
    path = download(src, settings.raw_dir, refresh)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    text, detected_version = extract(src, path)
    chunks = build_chunks(src, text)
    if not chunks:
        raise RuntimeError(f"{src.id}: no se obtuvo texto (¿PDF escaneado o página dinámica?)")

    embeddings = None
    if embedder is not None:
        texts = [f"{c['heading']}\n{c['text']}" for c in chunks]

        def progress(done: int, total: int) -> None:
            print(f"\r  embeddings {src.id}: {done}/{total}", end="", file=sys.stderr, flush=True)

        embeddings = embedder.embed_documents(texts, progress=progress)
        print(file=sys.stderr)

    store.replace_document(
        {
            "id": src.id,
            "title": src.title,
            "short_title": src.short_title,
            "institution": src.institution,
            "url": src.url,
            "version": src.version or detected_version,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sha256": sha,
        },
        chunks,
        embeddings,
    )
    log.info("%s: %d fragmentos en %.1fs", src.id, len(chunks), time.time() - t0)
    return len(chunks)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="Indexa fuentes oficiales para el RAG")
    ap.add_argument("--only", nargs="*", help="ids de fuentes a procesar")
    ap.add_argument("--refresh", action="store_true", help="volver a descargar los archivos")
    ap.add_argument("--no-embed", action="store_true", help="no calcular embeddings")
    args = ap.parse_args(argv)

    settings = get_settings()
    sources = load_sources(settings.sources_file)
    if args.only:
        unknown = set(args.only) - {s.id for s in sources}
        if unknown:
            ap.error(f"fuentes desconocidas: {', '.join(sorted(unknown))}")
        sources = [s for s in sources if s.id in args.only]

    embedder = None
    if not args.no_embed:
        embedder = OpenAICompatibleEmbedder(
            settings.effective_embed_base_url,
            settings.embed_model,
            api_key=settings.llm_api_key,
            batch_size=settings.embed_batch_size,
        )

    store = IndexStore(settings.index_path)
    failures = 0
    for src in sources:
        try:
            ingest_source(src, settings, store, embedder, refresh=args.refresh)
        except Exception as exc:  # noqa: BLE001 - seguir con las demás fuentes
            failures += 1
            log.error("%s: %s", src.id, exc)
    log.info("Índice: %s", store.stats())
    store.close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
