import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from app.limits import RequestLimits
from app.main import app
from .conftest import CARTA
from .test_retrieval import store  # índice con tres consultas distintas


async def echo(scope, receive, send):
    body = await receive()
    await JSONResponse({"size": len(body["body"])})(scope, receive, send)


async def test_limits_count_chunked_bodies_and_return_readable_error():
    guarded = RequestLimits(echo, max_bytes=6)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://test") as c:
        async def chunks():
            yield b"1234"
            yield b"5678"
        r = await c.post("/api/chat", content=chunks())
        assert r.status_code == 413 and r.json()["message"]
        assert (await c.post("/api/chat", content=b"123")).json()["size"] == 3


async def test_busy_stream_rejects_excess_and_releases_slot_when_cancelled():
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def slow(scope, receive, send):
        entered.set()
        await finish.wait()
        await echo(scope, receive, send)

    guarded = RequestLimits(slow, concurrent=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://test") as c:
        pending = asyncio.create_task(c.post("/api/chat", content=b"ok"))
        await asyncio.wait_for(entered.wait(), 2)
        busy = await c.post("/api/chat", content=b"ok")
        assert busy.status_code == 429 and busy.headers["retry-after"] == "60"
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        finish.set()
        assert (await c.post("/api/chat", content=b"ok")).status_code == 200


async def test_request_budget_also_applies_to_sequential_requests():
    guarded = RequestLimits(echo, per_minute=2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guarded), base_url="http://test") as c:
        assert (await c.post("/api/chat")).status_code == 200
        assert (await c.post("/api/documents")).status_code == 200
        assert (await c.post("/api/chat")).status_code == 429


def test_simultaneous_queries_and_status_do_not_mix_results(store):
    queries = ["vacaciones", "despido", "inscripcion rfc"]
    expected = {q: store.bm25(q, 2) for q in queries}

    def search(i):
        q = queries[i % len(queries)]
        assert store.bm25(q, 2) == expected[q]
        assert store.stats()["chunks"] == 3
        assert set(store.get_chunks(expected[q])) == set(expected[q])

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(search, range(240)))


async def test_document_expiry_and_cleanup(connector):
    doc = await connector.generate(connector.registry.by_tool("generar_carta_poder"), CARTA)
    assert connector.get(doc.id)
    old = time.time() - 25 * 3600
    for p in connector.storage_dir.iterdir():
        os.utime(p, (old, old))
    unrelated = connector.storage_dir / "keep.txt"
    unrelated.write_text("keep")
    os.utime(unrelated, (old, old))
    assert connector.get(doc.id) is None
    orphan = connector.storage_dir / ("a" * 32 + ".bin")
    orphan.write_bytes(b"old")
    os.utime(orphan, (old, old))
    connector.purge_expired()
    assert not orphan.exists() and unrelated.exists()


def test_oversized_document_fields_rejected_and_downloads_not_cached(connector):
    with TestClient(app) as c:
        c.app.state.connector = connector
        for value in ["a" * 4001, "\n" * 51]:
            r = c.post("/api/documents", json={"type": "carta_poder", "data": {**CARTA, "lugar": value}})
            assert r.status_code == 422
        doc = c.post("/api/documents", json={"type": "carta_poder", "data": CARTA}).json()
        assert c.get(doc["url"]).headers["cache-control"] == "private, no-store"
        assert c.get("/healthz").status_code == 200


async def test_inline_mock_avoids_second_copy(connector, monkeypatch):
    import mock_doc_service.main as mock
    monkeypatch.setenv("MOCK_DOC_INLINE", "true")
    doc = await connector.generate(connector.registry.by_tool("generar_carta_poder"), CARTA)
    assert connector.get(doc.id)[0].read_bytes().startswith(b"%PDF")
    assert not mock.STORAGE.exists()
