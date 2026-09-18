"""OFFLINE, SCRIPTED rehearsal traces of three failure paths (no Groq calls).

These traces exercise the real controller, registry and simulator, but the "model" is a
ScriptedModelClient replaying a fixed decision list for the fixture student IDs TEST-001 /
TEST-003 (never the submitting student's scenarios). They demonstrate controller mechanics
(denial -> re-plan -> escalate, stale world -> re-observe -> re-plan, failed verification ->
re-plan -> different fix) and are labelled as scripted everywhere they are used.

Usage: python scripts/offline_failure_demos.py   # writes traces/offline/*.jsonl
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incidentzero.agent.controller import AgentController  # noqa: E402
from incidentzero.approval.gateway import AlwaysApproveGateway, AlwaysDenyGateway  # noqa: E402
from incidentzero.domain.models import ModelReply, ToolCall  # noqa: E402
from incidentzero.environment.engine import SimulationEnvironment  # noqa: E402
from incidentzero.model.scripted import ScriptedModelClient  # noqa: E402
from incidentzero.telemetry.budget import BudgetManager  # noqa: E402
from incidentzero.telemetry.trace import TraceRecorder  # noqa: E402
from incidentzero.tools.registry import ToolRegistry  # noqa: E402

OUT = Path("traces/offline")


def call(name: str, **arguments) -> ModelReply:
    return ModelReply(content=None, tool_calls=[ToolCall(id=f"scripted-{name}", name=name, arguments=arguments)])


def plan(hypothesis: str, rationale: str = "scripted rehearsal plan") -> dict:
    return {"hypothesis": hypothesis, "rationale_summary": rationale, "steps": [
        {"step_id": "observe", "objective": "get_metrics/get_logs/get_deployments for the suspected service", "success_signal": "fault evidence"},
        {"step_id": "remediate", "objective": "least risky evidence-backed remediation", "success_signal": "tool ok"},
        {"step_id": "verify", "objective": "verify_recovery", "success_signal": "criteria_met=true"},
        {"step_id": "close", "objective": "close_incident citing verify evidence, else escalate", "success_signal": "terminal ok"}]}


DEMOS = {
    "scripted_denial_TEST-001_public-n": ("TEST-001", "public-n", AlwaysDenyGateway(), [plan("checkout-service 2.4.1 rollout is faulty"), plan("Faulty 2.4.1 release confirmed but rollback was denied; escalate with evidence", "approval denied")], [
        call("get_deployments", service="checkout-service"),
        call("get_metrics", service="checkout-service"),
        call("rollback_deployment", service="checkout-service", target_version="2.4.0", expected_world_version=1,
             reason="error rate rose right after the 2.4.1 rollout"),
        call("escalate_incident", reason="Rollback of checkout-service 2.4.1 was denied by the approver; no lower-risk fix "
             "addresses a faulty release. Handing off with evidence.", evidence_ids=["EV-0002", "EV-0003"]),
    ]),
    "scripted_stale_TEST-001_public-f": ("TEST-001", "public-f", AlwaysApproveGateway(), [plan("payment-service is failing"), plan("redis-cache corruption still present at the new world version; re-apply clear_cache", "world changed before the fix")], [
        call("get_metrics", service="redis-cache"),
        call("get_logs", service="redis-cache", limit=10),
        call("clear_cache", service="redis-cache", expected_world_version=1, reason="checksum mismatches in redis-cache logs"),
        call("clear_cache", service="redis-cache", expected_world_version=2, reason="fresh health read still shows corruption"),
        call("verify_recovery"),
        call("close_incident", summary="Cleared corrupted redis-cache entries after re-confirming on fresh evidence; verified.",
             evidence_ids=["EV-0003"], expected_world_version=3, reason="verified recovery"),
    ]),
    "scripted_verify_failed_TEST-001_public-a": ("TEST-001", "public-a", AlwaysApproveGateway(), [plan("cart-service holds bad session state"), plan("Restart was ineffective; the shared redis-cache may hold corrupted entries", "verification failed")], [
        call("get_metrics", service="cart-service"),
        call("restart_service", service="cart-service", expected_world_version=1, reason="cart sessions inconsistent"),
        call("verify_recovery"),
        call("get_metrics", service="redis-cache"),
        call("get_logs", service="redis-cache", limit=10),
        call("clear_cache", service="redis-cache", expected_world_version=2, reason="checksum mismatches in redis-cache logs"),
        call("verify_recovery"),
        call("close_incident", summary="Restart was ineffective; clearing corrupted redis-cache restored the checkout path.",
             evidence_ids=["EV-0008"], expected_world_version=3, reason="verified recovery"),
    ]),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (student, scenario, gateway, plans, decisions) in DEMOS.items():
        path = OUT / f"{name}.jsonl"
        path.unlink(missing_ok=True)
        controller = AgentController(ScriptedModelClient(decisions, plans), ToolRegistry(SimulationEnvironment(student, scenario)),
                                     gateway, BudgetManager(), TraceRecorder(path), sleeper=lambda s: None)
        outcome = controller.run()
        print(f"{name}: {outcome.status} (llm={outcome.llm_calls}, tools={outcome.tool_calls}) -> {path}")


if __name__ == "__main__":
    main()
