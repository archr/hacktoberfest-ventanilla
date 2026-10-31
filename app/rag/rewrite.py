"""Reescritura de la pregunta coloquial a una consulta con terminología jurídica/administrativa.

"me corrieron sin razón, ¿qué me deben pagar?" ->
"despido injustificado indemnización constitucional prima de antigüedad Ley Federal del Trabajo"
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ..llm import LLMError

log = logging.getLogger(__name__)

REWRITE_PROMPT = """\
Eres experto en derecho y trámites gubernamentales de México. Convierte el último mensaje de la \
persona (usando la conversación como contexto) en UNA consulta de búsqueda para encontrar artículos \
de leyes federales y fichas de trámites del SAT.
- Usa la terminología oficial (p. ej. "despido injustificado indemnización", "inscripción en el RFC \
de personas físicas", "certificado de e.firma", "constancia de situación fiscal").
- Incluye la ley o dependencia probable si la conoces (Ley Federal del Trabajo, Código Fiscal de la \
Federación, Ley del Impuesto sobre la Renta, Ley Federal de Protección al Consumidor, Ley del Seguro \
Social, SAT, etc.).
- Responde solo con la consulta en una línea, máximo 30 palabras, sin comillas ni explicaciones."""


class CompletionModel(Protocol):
    async def complete(self, messages: list[dict[str, Any]], max_tokens: int = 120) -> str: ...


class QueryRewriter:
    def __init__(self, llm: CompletionModel, turns: int = 4) -> None:
        self.llm = llm
        self.turns = turns

    async def rewrite(self, history: list[dict[str, Any]]) -> str | None:
        convo = [m for m in history if m["role"] in ("user", "assistant") and m.get("content")]
        lines = []
        for m in convo[-self.turns :]:
            who = "Persona" if m["role"] == "user" else "Asistente"
            lines.append(f"{who}: {m['content'][:600]}")
        messages = [
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": "Conversación:\n" + "\n".join(lines) + "\n\nConsulta de búsqueda:"},
        ]
        try:
            out = await self.llm.complete(messages, max_tokens=80)
        except LLMError as exc:
            log.warning("No se pudo reescribir la consulta: %s", exc)
            return None
        out = (out or "").strip().strip('"«»').strip()
        if not out:
            return None
        return out.splitlines()[0][:300]
