"""Clientes para el LLM y el modelo de embeddings (API compatible con OpenAI).

Funciona con Ollama (`/v1`), vLLM, LM Studio o llama.cpp server.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import numpy as np

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON crudo tal como lo produjo el modelo

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Argumentos JSON inválidos: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("Los argumentos deben ser un objeto JSON")
        return value

    def to_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class StreamChunk:
    """Evento normalizado del stream del LLM."""

    kind: str  # "reasoning" | "content" | "tool_calls" | "finish"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None


class ChatModel(Protocol):
    def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[StreamChunk]: ...


class OpenAICompatibleChat:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        temperature: float = 0.3,
        reasoning_effort: str | None = None,
        timeout_s: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 120,
        reasoning_effort: str | None = "none",
        timeout_s: float = 30.0,
    ) -> str:
        """Llamada corta sin streaming (p. ej. reescritura de consultas)."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        try:
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"No se pudo conectar con el LLM: {exc}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"El LLM respondió {resp.status_code}: {resp.text[:300]}")
        return (resp.json()["choices"][0]["message"].get("content") or "").strip()

    async def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[StreamChunk]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        headers = {"Authorization": f"Bearer {self.api_key}"}
        acc: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode(errors="replace")[:500]
                    raise LLMError(f"El LLM respondió {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    data = json.loads(payload)
                    if data.get("error"):
                        raise LLMError(str(data["error"]))
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                        if reasoning:
                            yield StreamChunk("reasoning", text=reasoning)
                        if delta.get("content"):
                            yield StreamChunk("content", text=delta["content"])
                        for tc in delta.get("tool_calls") or []:
                            slot = acc.setdefault(
                                tc.get("index", len(acc)), {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except httpx.HTTPError as exc:
            raise LLMError(f"No se pudo conectar con el LLM en {self.base_url}: {exc}") from exc

        if acc:
            calls = [
                ToolCall(id=s["id"] or f"call_{i}", name=s["name"], arguments=s["arguments"])
                for i, s in sorted(acc.items())
            ]
            yield StreamChunk("tool_calls", tool_calls=calls)
        yield StreamChunk("finish", finish_reason=finish_reason)


class Embedder(Protocol):
    async def embed_queries(self, texts: list[str]) -> np.ndarray: ...


class OpenAICompatibleEmbedder:
    """Embeddings normalizados (L2) para búsqueda por similitud coseno."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "ollama",
        query_instruction: str = "",
        batch_size: int = 32,
        timeout_s: float = 120.0,
    ) -> None:
        self.url = base_url.rstrip("/") + "/embeddings"
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.query_instruction = query_instruction
        self.batch_size = batch_size
        self.timeout_s = timeout_s

    @staticmethod
    def _normalize(vectors: list[list[float]]) -> np.ndarray:
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    def embed_documents(self, texts: list[str], progress=None) -> np.ndarray:
        out: list[np.ndarray] = []
        with httpx.Client(timeout=self.timeout_s) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                resp = client.post(
                    self.url, json={"model": self.model, "input": batch}, headers=self.headers
                )
                if resp.status_code >= 400:
                    raise LLMError(f"Embeddings {resp.status_code}: {resp.text[:300]}")
                data = sorted(resp.json()["data"], key=lambda d: d["index"])
                out.append(self._normalize([d["embedding"] for d in data]))
                if progress:
                    progress(min(start + self.batch_size, len(texts)), len(texts))
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)

    async def embed_queries(self, texts: list[str]) -> np.ndarray:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.url,
                json={"model": self.model, "input": [self.query_instruction + t for t in texts]},
                headers=self.headers,
            )
        if resp.status_code >= 400:
            raise LLMError(f"Embeddings {resp.status_code}: {resp.text[:300]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return self._normalize([d["embedding"] for d in data])
