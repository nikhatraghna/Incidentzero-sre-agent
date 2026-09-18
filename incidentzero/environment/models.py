from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceState:
    name: str
    healthy: bool = True
    replicas: int = 2
    version: str = "1.0.0"
    previous_version: str = "0.9.9"
    error_rate: float = 0.005
    p95_ms: int = 180
    cpu_pct: float = 35.0
    memory_pct: float = 42.0
    cache_hit_rate: float | None = None
    db_connections_pct: float | None = None
    logs: list[str] = field(default_factory=list)


@dataclass
class IncidentSpec:
    incident_id: str
    title: str
    customer_impact: str
    severity: str
    suspected_service: str
    misleading_hint: str
    family: str
    target_service: str
    created_at: str = "2026-09-11T09:00:00Z"
    failure_plan: dict[str, int] = field(default_factory=dict)
    event_at_tool_call: int | None = None
    event_type: str | None = None
    impossible: bool = False
    parameters: dict[str, Any] = field(default_factory=dict)
