"""Configuración de la aplicación (variables de entorno / archivo .env)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM (cualquier servidor compatible con la API de OpenAI: Ollama, vLLM, LM Studio…)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen3.8:27b-mlx"
    llm_reasoning_effort: str | None = "none"  # "" -> por defecto del modelo
    llm_temperature: float = 0.3
    llm_timeout_s: float = 300.0

    # Embeddings
    embed_base_url: str | None = None
    embed_model: str = "qwen3-embedding:0.6b"
    embed_query_instruction: str = (
        "Instruct: Dada la pregunta de una persona sobre trámites o leyes de México, "
        "recupera los fragmentos de fuentes oficiales que la responden\nQuery: "
    )
    embed_batch_size: int = 32

    # RAG
    data_dir: Path = BASE_DIR / "data"
    sources_file: Path = BASE_DIR / "config" / "sources.yaml"
    rag_top_k: int = 6
    rag_candidates: int = 40
    rag_max_per_section: int = 2
    rag_query_rewrite: bool = True  # el LLM reformula la pregunta con terminología jurídica

    # Documentos
    documents_file: Path = BASE_DIR / "config" / "document_services.yaml"
    max_document_bytes: int = 20 * 1024 * 1024
    confirm_documents: bool = True  # la persona confirma los datos en una tarjeta antes de enviarlos

    # Chat
    max_history_messages: int = 20
    max_tool_rounds: int = 3
    timezone: str = "America/Mexico_City"
    max_request_bytes: int = 65536
    max_concurrent_requests: int = 2
    requests_per_minute: int = 30
    document_retention_hours: float = 24
    demo_mode: bool = False

    @field_validator("llm_reasoning_effort", "embed_base_url", mode="before")
    @classmethod
    def _empty_to_none(cls, v):
        return v or None

    @field_validator("data_dir", "sources_file", "documents_file", mode="after")
    @classmethod
    def _resolve(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BASE_DIR / v)

    @property
    def effective_embed_base_url(self) -> str:
        return self.embed_base_url or self.llm_base_url

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.sqlite"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def generated_dir(self) -> Path:
        return self.data_dir / "generated"


def load_env_file(path: Path = BASE_DIR / ".env") -> None:
    """Exporta .env a os.environ (sin sobrescribir) para las variables ${VAR} de los YAML."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@lru_cache
def get_settings() -> Settings:
    load_env_file()
    return Settings()
