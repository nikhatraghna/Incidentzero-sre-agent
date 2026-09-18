from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incidentzero.domain.models import RiskLevel
from incidentzero.tools.definitions import TOOLS

from .config import resolve_config_path

# ---------------------------------------------------------------------------------------
# Tool categories, derived structurally from the tool schemas (not from risk levels, which
# always come from configs/risk_policy.json at run time).
# ---------------------------------------------------------------------------------------
ALL_TOOL_NAMES: tuple[str, ...] = tuple(spec["function"]["name"] for spec in TOOLS)
VERSIONED_TOOLS = frozenset(
    spec["function"]["name"] for spec in TOOLS
    if "expected_world_version" in spec["function"]["parameters"].get("properties", {})
)
TERMINAL_TOOLS = frozenset({"close_incident", "escalate_incident"})
MUTATING_TOOLS = VERSIONED_TOOLS | {"escalate_incident"}
REMEDIATION_TOOLS = VERSIONED_TOOLS - TERMINAL_TOOLS
OBSERVATION_TOOLS = frozenset(ALL_TOOL_NAMES) - MUTATING_TOOLS  # read-only, incl. verify_recovery
SYMPTOM_TOOLS = frozenset({"get_metrics", "get_logs", "get_service_health"})


class RiskPolicy:
    """Risk levels are read from ``configs/risk_policy.json`` when the policy is built, so a
    TA can change a level (e.g. restart_service -> high) and the next run obeys it without
    code edits. Unknown tools and unknown level strings fail safe to CRITICAL."""

    def __init__(self, config_path: str | Path = "configs/risk_policy.json") -> None:
        self.config_path = resolve_config_path(config_path)
        self.mapping = json.loads(self.config_path.read_text(encoding="utf-8"))

    def risk(self, tool_name: str) -> RiskLevel:
        try:
            return RiskLevel(str(self.mapping.get(tool_name, "critical")).strip().lower())
        except ValueError:
            return RiskLevel.CRITICAL

    def requires_human_approval(self, tool_name: str) -> bool:
        return self.risk(tool_name) in {RiskLevel.HIGH, RiskLevel.CRITICAL}


# ---------------------------------------------------------------------------------------
# Re-planning triggers (R4). Each trigger is named so the controller, the planner prompt
# and the trace can treat them differently; should_replan() keeps the bool contract.
# ---------------------------------------------------------------------------------------
TRIGGER_STALE = "stale_precondition"
TRIGGER_APPROVAL_DENIED = "approval_denied"
TRIGGER_ACTION_FAILED = "non_retryable_action_failure"
TRIGGER_VERIFICATION_FAILED = "verification_failed"
TRIGGER_CONTRADICTION = "contradictory_evidence"
TRIGGER_LOOP = "loop_detected"
TRIGGER_BUDGET = "budget_low"
TRIGGER_PRIORITY: tuple[str, ...] = (
    TRIGGER_APPROVAL_DENIED, TRIGGER_STALE, TRIGGER_ACTION_FAILED, TRIGGER_VERIFICATION_FAILED,
    TRIGGER_CONTRADICTION, TRIGGER_LOOP, TRIGGER_BUDGET,
)
# Statuses the controller synthesises for events that do not come from the simulator.
_SYNTHETIC_STATUS = {
    "loop_blocked": TRIGGER_LOOP,
    "budget_low": TRIGGER_BUDGET,
    "contradictory_evidence": TRIGGER_CONTRADICTION,
}


class ReplanPolicy:
    """Classifies an observation into a named re-plan trigger (or None).

    * stale_precondition      -> the world changed: re-observe, then re-confirm the plan
    * approval_denied         -> the human said no: never retry blindly; alternative or escalate
    * non-retryable failure   -> an action (mutating tool) failed permanently
    * verification_failed     -> verify_recovery says criteria_met=false after a remediation
    * contradictory_evidence / loop_detected / budget_low -> controller-detected triggers
    * transient errors are *retried*, validation errors get *corrective feedback*, and a
      failed read-only observation is corrected rather than re-planned: none re-plan.
    """

    def classify(self, tool_result: dict[str, Any], *, after_remediation: bool | None = None) -> str | None:
        if not isinstance(tool_result, dict):
            return None
        status = tool_result.get("status")
        if status == "stale_precondition":
            return TRIGGER_STALE
        if status == "approval_denied":
            return TRIGGER_APPROVAL_DENIED
        if status in _SYNTHETIC_STATUS:
            return _SYNTHETIC_STATUS[status]
        tool = tool_result.get("tool")
        if status == "ok":
            if tool == "verify_recovery":
                data = tool_result.get("data") or {}
                # Without context (after_remediation=None) a failed verification is treated
                # as a trigger; the controller passes False for a pre-remediation baseline.
                if data.get("criteria_met") is False and after_remediation is not False:
                    return TRIGGER_VERIFICATION_FAILED
            return None
        if status == "error" and not tool_result.get("retryable", False):
            if tool is None or tool in MUTATING_TOOLS:
                return TRIGGER_ACTION_FAILED
        return None

    def should_replan(self, tool_result: dict[str, Any], **context: Any) -> bool:
        return self.classify(tool_result, **context) is not None


@dataclass(frozen=True)
class RecoveryObjective:
    """Recovery thresholds. Parsed from the incident's own objective text
    ("Restore checkout success >=99%, p95 <=800ms ...") with conservative fallbacks."""

    min_success_rate: float = 0.99
    max_p95_ms: float = 800.0

    @classmethod
    def from_incident(cls, incident_data: dict[str, Any] | None) -> "RecoveryObjective":
        text = str((incident_data or {}).get("objective", ""))
        success = re.search(r"success\s*>=?\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
        p95 = re.search(r"p95\s*<=?\s*(\d+(?:\.\d+)?)\s*ms", text, re.IGNORECASE)
        return cls(
            min_success_rate=float(success.group(1)) / 100.0 if success else cls.min_success_rate,
            max_p95_ms=float(p95.group(1)) if p95 else cls.max_p95_ms,
        )


def observed_health(result: dict[str, Any], objective: RecoveryObjective) -> bool | None:
    """True = observation shows the service healthy/within objective, False = unhealthy,
    None = the observation carries no health signal (or failed)."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    data = result.get("data") or {}
    tool = result.get("tool")
    if tool == "get_service_health":
        healthy = data.get("healthy")
        return healthy if isinstance(healthy, bool) else None
    if tool == "get_metrics":
        err, p95 = data.get("error_rate"), data.get("p95_ms")
        if not isinstance(err, (int, float)) or not isinstance(p95, (int, float)):
            return None
        return err <= (1.0 - objective.min_success_rate) + 1e-9 and p95 <= objective.max_p95_ms
    return None


# ---------------------------------------------------------------------------------------
# Loop detection (R7)
# ---------------------------------------------------------------------------------------
VOLATILE_ARGUMENTS = frozenset({"expected_world_version", "reason", "summary", "evidence_ids"})


class LoopGuard:
    """Detects repeated actions.

    ``record(action, args)`` is the exact-repeat contract: a stable fingerprint
    (name + canonical JSON of the arguments) whose count exceeding
    ``max_same_action_repeats`` returns True (with the default 2: call 3 is a loop).
    The controller feeds observations with the current world_version folded into the
    arguments, so re-reading a service after the world changed is new evidence, not a loop.

    The *semantic* layer ignores volatile fields (version, reason text, evidence list) so the
    same remediation re-issued with a bumped version still counts as the same action; it is
    counted on execution and can be reset when the world changes for external reasons.
    """

    def __init__(self, max_same_action_repeats: int = 2) -> None:
        self.max_same_action_repeats = max_same_action_repeats
        self._counts: dict[str, int] = {}
        self._semantic_counts: dict[str, int] = {}

    @staticmethod
    def fingerprint(action_name: str, arguments: Any) -> str:
        args = arguments if isinstance(arguments, dict) else {"__raw__": repr(arguments)}
        return json.dumps({"action": action_name, "arguments": args}, sort_keys=True,
                          separators=(",", ":"), default=str)

    def record(self, action_name: str, arguments: dict[str, Any]) -> bool:
        key = self.fingerprint(action_name, arguments)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key] > self.max_same_action_repeats

    def count(self, action_name: str, arguments: dict[str, Any]) -> int:
        return self._counts.get(self.fingerprint(action_name, arguments), 0)

    @staticmethod
    def semantic_arguments(arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return {"__raw__": repr(arguments)}
        return {k: v for k, v in arguments.items() if k not in VOLATILE_ARGUMENTS}

    def semantic_key(self, action_name: str, arguments: Any) -> str:
        return self.fingerprint(action_name, self.semantic_arguments(arguments))

    def semantic_count(self, action_name: str, arguments: Any) -> int:
        return self._semantic_counts.get(self.semantic_key(action_name, arguments), 0)

    def would_exceed_semantic(self, action_name: str, arguments: Any) -> bool:
        return self.semantic_count(action_name, arguments) + 1 > self.max_same_action_repeats

    def note_executed(self, action_name: str, arguments: Any) -> None:
        key = self.semantic_key(action_name, arguments)
        self._semantic_counts[key] = self._semantic_counts.get(key, 0) + 1

    def reset_semantic(self) -> None:
        self._semantic_counts.clear()
