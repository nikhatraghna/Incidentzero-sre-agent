from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from incidentzero.domain.models import AgentPlan


@dataclass
class AgentState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    plan: AgentPlan | None = None
    evidence_ids: list[str] = field(default_factory=list)
    latest_world_version: int | None = None
    last_tool_results: list[dict[str, Any]] = field(default_factory=list)
    repeated_actions: dict[str, int] = field(default_factory=dict)
    status: str = "running"
    # ---- structured working memory added for the reliable controller ----
    incident: dict[str, Any] | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)       # ok read-only results, in order
    latest_by_service: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)  # service -> tool -> result
    latest_verify: dict[str, Any] | None = None                             # latest ok verify_recovery result
    remediations: list[dict[str, Any]] = field(default_factory=list)       # executed remediation actions
    mutations_since_verify: int = 0
    reobserve_required: bool = False
    stale_events: int = 0
    replan_triggers: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    denied_actions: dict[str, dict[str, Any]] = field(default_factory=dict)  # semantic key -> info
    blocked_actions: list[dict[str, Any]] = field(default_factory=list)
    unproductive_streak: int = 0
    wrap_up: bool = False
    token_usage: dict[str, int] = field(default_factory=dict)

    def observe_result(self, result: dict[str, Any]) -> None:
        evidence = result.get("evidence_id")
        if evidence:
            self.evidence_ids.append(evidence)
        version = result.get("world_version")
        if isinstance(version, int):
            self.latest_world_version = version
        self.last_tool_results.append(result)
        self.last_tool_results = self.last_tool_results[-8:]

    def record_observation(self, tool: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        """Index a successful read-only observation (called for ok results of read-only tools)."""
        self.observations.append(result)
        service = (result.get("data") or {}).get("service") or arguments.get("service")
        if isinstance(service, str):
            self.latest_by_service.setdefault(service, {})[tool] = result
        if tool == "verify_recovery":
            self.latest_verify = result
            self.mutations_since_verify = 0
        if result.get("world_version") == self.latest_world_version:
            self.reobserve_required = False

    def latest_for(self, service: str, tool: str) -> dict[str, Any] | None:
        return self.latest_by_service.get(service, {}).get(tool)

    def has_evidence_for(self, service: str, tools: frozenset[str] | set[str]) -> bool:
        return any(tool in tools for tool in self.latest_by_service.get(service, {}))

    def proof_of_recovery(self) -> dict[str, Any] | None:
        """The latest verify_recovery result if it proves recovery at the current world version."""
        verify = self.latest_verify
        if not verify or not (verify.get("data") or {}).get("criteria_met"):
            return None
        if verify.get("world_version") != self.latest_world_version or self.mutations_since_verify:
            return None
        return verify
