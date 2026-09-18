from __future__ import annotations

from typing import Any, Protocol

from incidentzero.domain.models import ModelReply


class ModelClient(Protocol):
    def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        ...

    def structured(self, messages: list[dict[str, Any]], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
        ...
