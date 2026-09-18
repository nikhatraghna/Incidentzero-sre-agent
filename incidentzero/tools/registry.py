from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from incidentzero.environment.engine import SimulationEnvironment
from .definitions import TOOL_BY_NAME, TOOLS


class ToolRegistry:
    def __init__(self, environment: SimulationEnvironment) -> None:
        self.environment = environment

    @property
    def groq_tools(self) -> list[dict[str, Any]]:
        return TOOLS

    def validate(self, name: str, arguments: dict[str, Any]) -> tuple[bool, str | None]:
        spec = TOOL_BY_NAME.get(name)
        if not spec:
            return False, f"Unknown tool: {name}"
        schema = spec["function"]["parameters"]
        errors = sorted(Draft202012Validator(schema).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            return False, "; ".join(error.message for error in errors[:3])
        return True, None

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ok, error = self.validate(name, arguments)
        if not ok:
            return {
                "status": "validation_error", "tool": name, "world_version": self.environment.world_version,
                "evidence_id": None, "data": None, "retryable": False, "message": error,
            }
        return self.environment.execute(name, arguments)
