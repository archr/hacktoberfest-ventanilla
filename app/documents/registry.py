"""Registro de tipos de documento y servicios externos (config/document_services.yaml)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")
TOOL_PREFIX = "generar_"


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class ServiceConfig:
    name: str
    base_url: str
    auth_type: str = "none"  # none | bearer | header
    token_env: str | None = None
    header_name: str = "X-API-Key"
    timeout_s: float = 60.0

    def auth_headers(self) -> dict[str, str]:
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        if not token or self.auth_type == "none":
            return {}
        if self.auth_type == "bearer":
            return {"Authorization": f"Bearer {token}"}
        if self.auth_type == "header":
            return {self.header_name: token}
        raise ValueError(f"Tipo de auth desconocido: {self.auth_type}")


@dataclass
class DocumentType:
    id: str
    title: str
    description: str
    service: str
    parameters: dict[str, Any]
    method: str = "POST"
    path: str = "/v1/documents"
    _validator: Draft202012Validator | None = field(default=None, repr=False)

    @property
    def tool_name(self) -> str:
        return f"{TOOL_PREFIX}{self.id}"

    @property
    def required(self) -> list[str]:
        return list(self.parameters.get("required", []))

    def tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": (
                    f"Genera el documento «{self.title}». {self.description} "
                    "Úsala SOLO si la persona pidió explícitamente este documento y ya tienes los datos "
                    "obligatorios. Antes de enviarse, la persona revisará y confirmará los datos en pantalla."
                ),
                "parameters": self.parameters,
            },
        }

    def fields(self) -> list[dict[str, Any]]:
        """Descripción de los campos para el formulario de confirmación en la UI."""
        out = []
        required = set(self.required)
        for name, spec in self.parameters.get("properties", {}).items():
            out.append(
                {
                    "name": name,
                    "label": spec.get("description") or name,
                    "type": spec.get("type", "string"),
                    "enum": spec.get("enum"),
                    "required": name in required,
                }
            )
        return out

    def validate(self, data: dict[str, Any]) -> list[str]:
        if self._validator is None:
            self._validator = Draft202012Validator(self.parameters)
        props = self.parameters.get("properties", {})
        errors: list[str] = []
        for name in self.required:
            value = data.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                desc = props.get(name, {}).get("description", "")
                errors.append(f"Falta el dato obligatorio '{name}'" + (f" ({desc})" if desc else ""))
        for err in self._validator.iter_errors(data):
            if err.validator == "required":
                continue  # ya reportado arriba con mejor mensaje
            where = ".".join(str(p) for p in err.absolute_path) or "datos"
            errors.append(f"'{where}': {err.message}")
        return errors


class DocumentRegistry:
    def __init__(self, services: dict[str, ServiceConfig], documents: list[DocumentType]) -> None:
        self.services = services
        self.documents = documents
        self._by_tool = {d.tool_name: d for d in documents}
        for d in documents:
            if d.service not in services:
                raise ValueError(f"El documento '{d.id}' usa el servicio desconocido '{d.service}'")

    @classmethod
    def from_file(cls, path: Path) -> "DocumentRegistry":
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data = _expand_env(raw)
        services = {}
        for name, cfg in (data.get("services") or {}).items():
            auth = cfg.get("auth") or {}
            services[name] = ServiceConfig(
                name=name,
                base_url=cfg["base_url"].rstrip("/"),
                auth_type=auth.get("type", "none"),
                token_env=auth.get("token_env"),
                header_name=auth.get("header_name", "X-API-Key"),
                timeout_s=float(cfg.get("timeout_s", 60)),
            )
        documents = []
        for item in data.get("documents") or []:
            req = item.get("request") or {}
            documents.append(
                DocumentType(
                    id=item["id"],
                    title=item["title"],
                    description=" ".join(str(item.get("description", "")).split()),
                    service=item["service"],
                    parameters=item["parameters"],
                    method=req.get("method", "POST").upper(),
                    path=req.get("path", "/v1/documents"),
                )
            )
        return cls(services, documents)

    def tools(self) -> list[dict[str, Any]]:
        return [d.tool_schema() for d in self.documents]

    def by_tool(self, name: str) -> DocumentType | None:
        return self._by_tool.get(name)

    def summary(self) -> list[dict[str, str]]:
        return [{"id": d.id, "title": d.title, "description": d.description} for d in self.documents]
