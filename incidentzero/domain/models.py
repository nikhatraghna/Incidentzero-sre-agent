from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ModelReply:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None


@dataclass(slots=True)
class PlanStep:
    step_id: str
    objective: str
    success_signal: str
    status: str = "pending"


@dataclass(slots=True)
class AgentPlan:
    hypothesis: str
    steps: list[PlanStep]
    revision: int = 0
    rationale_summary: str = ""


@dataclass(slots=True)
class AgentOutcome:
    status: str
    summary: str
    llm_calls: int
    tool_calls: int
    final_world_version: int | None
    evidence_ids: list[str] = field(default_factory=list)
    trace_path: str | None = None
