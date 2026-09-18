from __future__ import annotations

from collections import deque
from typing import Any

from incidentzero.domain.models import ModelReply


class ScriptedModelClient:
    """Offline deterministic model adapter for unit tests.

    The assignment grader can replace the real LLM with scripted/adversarial replies.
    Your controller must therefore contain the reliability logic; it must not rely on
    Groq magically behaving perfectly.
    """

    def __init__(self, decisions: list[ModelReply] | None = None, structured_outputs: list[dict] | None = None) -> None:
        self.decisions = deque(decisions or [])
        self.structured_outputs = deque(structured_outputs or [])

    def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        if not self.decisions:
            return ModelReply(content="No scripted decision remains.")
        return self.decisions.popleft()

    def structured(self, messages: list[dict[str, Any]], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
        if not self.structured_outputs:
            raise RuntimeError("No scripted structured output remains.")
        return self.structured_outputs.popleft()
