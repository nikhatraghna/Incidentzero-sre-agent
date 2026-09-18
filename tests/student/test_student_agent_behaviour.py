"""Student tests (Task G) for the IncidentZero controller.

All tests are deterministic and offline: they drive the real controller, registry and local
simulator with ScriptedModelClient-style models, AlwaysApprove/AlwaysDeny or recording
gateways, and fixed student IDs (TEST-00x) whose scenario families are fixed by the public
scenario factory. No test calls the Groq API.

Failure classes covered: transient model failure, permanent model failure, malformed/invalid
tool calls, schema violations, approval denial, dynamic risk policy, stale world state, loop
detection, premature/unverified closure, failed verification, LLM/tool/runtime budget
exhaustion, transient telemetry failure and an impossible (external dependency) incident.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from incidentzero.agent.controller import AgentController
from incidentzero.agent.planner import Planner, PlanOutputError
from incidentzero.agent.policies import (
    TRIGGER_ACTION_FAILED, TRIGGER_APPROVAL_DENIED, TRIGGER_STALE, TRIGGER_VERIFICATION_FAILED, ReplanPolicy,
    RiskPolicy,
)
from incidentzero.agent.recovery import RetryPolicy, retry_after_seconds
from incidentzero.approval.gateway import AlwaysApproveGateway, AlwaysDenyGateway, ApprovalGateway
from incidentzero.domain.models import ModelReply, ToolCall
from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.model.errors import PermanentModelError, TransientModelError
from incidentzero.model.scripted import ScriptedModelClient
from incidentzero.telemetry.budget import BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]

PLAN = {
    "hypothesis": "Unconfirmed: the ticket's suspected service may not be the root cause.",
    "rationale_summary": "Observe first, then remediate the component the evidence points to.",
    "steps": [
        {"step_id": "observe", "objective": "get_metrics and get_logs for the unhealthy service", "success_signal": "fault identified"},
        {"step_id": "remediate", "objective": "apply the least risky evidence-backed remediation", "success_signal": "tool ok"},
        {"step_id": "verify", "objective": "verify_recovery", "success_signal": "criteria_met=true"},
        {"step_id": "close", "objective": "close_incident citing the verify evidence", "success_signal": "close ok"},
    ],
}


# --------------------------------------------------------------------------- helpers
def call(name: str, **arguments) -> ModelReply:
    return ModelReply(content=None, tool_calls=[ToolCall(id=f"call-{name}", name=name, arguments=arguments)])


class SpyRegistry(ToolRegistry):
    """Real registry that also records what actually reached the simulator."""

    def __init__(self, env: SimulationEnvironment) -> None:
        super().__init__(env)
        self.executed: list[tuple[str, dict]] = []

    def execute(self, name, arguments):
        self.executed.append((name, dict(arguments)))
        return super().execute(name, arguments)

    def names(self) -> list[str]:
        return [name for name, _ in self.executed]


class RecordingGateway(ApprovalGateway):
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.requests: list[tuple[str, dict, str]] = []

    def approve(self, action, arguments, justification):
        self.requests.append((action, arguments, justification))
        return self.answer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build(tmp_path, student, scenario, decisions, *, gateway=None, budget=None, model=None, structured=None,
          clock=None):
    env = SimulationEnvironment(student, scenario)
    registry = SpyRegistry(env)
    trace_path = tmp_path / f"{student}_{scenario}.jsonl"
    model = model or ScriptedModelClient(list(decisions), list(structured if structured is not None else [PLAN]))
    kwargs = {"sleeper": lambda seconds: None}
    if clock is not None:
        kwargs["clock"] = clock
    controller = AgentController(model, registry, gateway or AlwaysApproveGateway(), budget or BudgetManager(),
                                 TraceRecorder(trace_path), **kwargs)
    return controller, registry, trace_path


def events(trace_path: Path) -> list[tuple[str, dict]]:
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [(row["event"], row["payload"]) for row in rows]


def names(trace_path: Path) -> list[str]:
    return [event for event, _ in events(trace_path)]


def tool_results(trace_path: Path, tool: str) -> list[dict]:
    return [p for e, p in events(trace_path) if e == "tool_result" and p["call"]["name"] == tool]


# --------------------------------------------------------------------------- recovery / retry
@pytest.mark.student
def test_retry_backoff_is_exponential_capped_and_honours_retry_after():
    sleeps: list[float] = []
    policy = RetryPolicy(max_attempts=5, sleeper=sleeps.append, base_delay=0.5, multiplier=2.0, max_delay=3.0)
    errors = [TransientModelError("503"), TransientModelError("429 Please try again in 2.5s"),
              TransientModelError("503"), TransientModelError("503")]

    def flaky():
        if errors:
            raise errors.pop(0)
        return "ok"

    assert policy.call_model(flaky) == "ok"
    assert sleeps == [0.5, 2.5, 2.0, 3.0]  # exponential, Retry-After honoured, capped at max_delay
    hinted = TransientModelError("rate limited")
    hinted.retry_after = 1.7
    assert retry_after_seconds(hinted) == 1.7
    # a retry that would overrun the runtime budget is not attempted
    no_time = RetryPolicy(max_attempts=3, sleeper=sleeps.append, time_remaining=lambda: 0.1)
    with pytest.raises(TransientModelError):
        no_time.call_model(lambda: (_ for _ in ()).throw(TransientModelError("503")))
    assert no_time.attempts_used == 1


@pytest.mark.student
def test_transient_model_errors_are_retried_boundedly_then_abort(tmp_path):
    class DownModel(ScriptedModelClient):
        def decide(self, messages, tools):
            raise TransientModelError("503 service unavailable")

    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", [], model=DownModel([], [PLAN]))
    outcome = controller.run()
    assert outcome.status == "aborted"
    assert outcome.llm_calls == 1 + 3  # plan + three bounded decide attempts, every attempt charged
    retries = [p for e, p in events(trace) if e == "retry"]
    assert [p["retry_count"] for p in retries] == [1, 2]
    assert registry.names() == ["get_incident"]  # no action taken while the model is unavailable


@pytest.mark.student
def test_permanent_model_error_aborts_without_retry_or_actions(tmp_path):
    class BadKeyModel(ScriptedModelClient):
        def decide(self, messages, tools):
            raise PermanentModelError("401 invalid api key")

    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", [], model=BadKeyModel([], [PLAN]))
    outcome = controller.run()
    assert outcome.status == "aborted"
    assert outcome.llm_calls == 2
    assert "retry" not in names(trace)
    assert registry.names() == ["get_incident"]


@pytest.mark.student
def test_every_model_attempt_is_traced_and_charged(tmp_path):
    class FlakyOnce(ScriptedModelClient):
        failed = False

        def decide(self, messages, tools):
            if not self.failed:
                self.failed = True
                raise TransientModelError("429 try again in 0.1s")
            return super().decide(messages, tools)

    model = FlakyOnce([call("get_metrics", service="redis-cache")], [PLAN])
    controller, _, trace = build(tmp_path, "TEST-001", "public-a", [], model=model)
    outcome = controller.run()
    requests = [p for e, p in events(trace) if e == "model_request"]
    assert len(requests) == outcome.llm_calls  # nothing reaches the model without being budgeted and traced
    assert any(e == "model_error" for e, _ in events(trace))


# --------------------------------------------------------------------------- validation boundary (R8)
@pytest.mark.student
def test_malformed_unknown_tool_and_unknown_service_never_reach_simulator(tmp_path):
    decisions = [
        ModelReply(tool_calls=[ToolCall("a", "get_metrics", {"__malformed_arguments__": "{service: redis"})]),
        call("delete_database", service="order-db"),
        call("get_metrics", service="billing-service"),
    ]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    outcome = controller.run()
    assert registry.names() == ["get_incident"]
    codes = [p.get("code") for e, p in events(trace) if e == "validation_failure"]
    assert {"malformed_arguments", "unknown_tool", "unknown_service"} <= set(codes)
    assert outcome.status == "failed"  # repeated invalid output ends the run without claiming success
    tool_messages = [m for m in controller.state.messages if m["role"] == "tool"]
    assert all(json.loads(m["content"])["status"] == "validation_error" for m in tool_messages)


@pytest.mark.student
def test_schema_violation_is_rejected_before_execution(tmp_path):
    decisions = [
        call("shift_traffic", service="checkout-service", percent=33, expected_world_version=1, reason="drain a bad zone"),
        call("get_logs", service="redis-cache"),  # required limit missing
    ]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    controller.run()
    assert "shift_traffic" not in registry.names() and "get_logs" not in registry.names()
    assert [p["code"] for e, p in events(trace) if e == "validation_failure"][:2] == ["schema_violation", "schema_violation"]


# --------------------------------------------------------------------------- approval (R3)
@pytest.mark.student
def test_denied_high_risk_action_is_not_executed_and_triggers_replan(tmp_path):
    decisions = [call("rollback_deployment", service="checkout-service", target_version="2.4.0",
                      expected_world_version=1, reason="errors began right after the 2.4.1 rollout")]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-n", decisions, gateway=AlwaysDenyGateway())
    outcome = controller.run()
    assert "rollback_deployment" not in registry.names()
    assert registry.execute("get_service_health", {"service": "checkout-service"})["data"]["version"] == "2.4.1"
    evs = events(trace)
    request = next(p for e, p in evs if e == "approval_request")
    assert "EV-" in request["justification"] and request["risk"] == "high"
    assert any(e == "replan_trigger" and p["trigger"] == TRIGGER_APPROVAL_DENIED for e, p in evs)
    assert controller.state.plan.revision >= 1
    assert outcome.status != "resolved"


@pytest.mark.student
def test_denied_action_is_not_requested_again_without_new_evidence(tmp_path):
    rollback = call("rollback_deployment", service="checkout-service", target_version="2.4.0",
                    expected_world_version=1, reason="errors began right after the 2.4.1 rollout")
    gateway = RecordingGateway(False)
    controller, registry, trace = build(tmp_path, "TEST-001", "public-n", [rollback, rollback], gateway=gateway)
    controller.run()
    assert len(gateway.requests) == 1
    assert any(e == "policy_rejection" and p["code"] == "previously_denied" for e, p in events(trace))
    assert "rollback_deployment" not in registry.names()


@pytest.mark.student
def test_risk_policy_change_requires_approval_without_code_changes(tmp_path):
    assert RiskPolicy().requires_human_approval("restart_service") is False  # shipped policy: medium
    policy = json.loads((REPO_ROOT / "configs" / "risk_policy.json").read_text(encoding="utf-8"))
    policy["restart_service"] = "high"
    custom = tmp_path / "risk_policy.json"
    custom.write_text(json.dumps(policy), encoding="utf-8")
    gateway = RecordingGateway(False)
    decisions = [
        call("get_service_health", service="inventory-service"),
        call("restart_service", service="inventory-service", expected_world_version=1, reason="memory climbing towards OOM"),
    ]
    controller, registry, _ = build(tmp_path, "TEST-001", "public-b", decisions, gateway=gateway)
    controller.risk = RiskPolicy(custom)
    controller.run()
    assert [r[0] for r in gateway.requests] == ["restart_service"]
    assert "restart_service" not in registry.names()


# --------------------------------------------------------------------------- stale world (R4/R5)
@pytest.mark.student
def test_stale_world_triggers_reobservation_and_replan_not_blind_retry(tmp_path):
    # TEST-001/public-f: cache corruption; a scheduled world event fires on the 4th tool call.
    decisions = [
        call("get_metrics", service="redis-cache"),
        call("get_logs", service="redis-cache", limit=10),
        call("clear_cache", service="redis-cache", expected_world_version=1, reason="checksum mismatches in redis logs"),
        call("clear_cache", service="redis-cache", expected_world_version=2, reason="re-confirmed on fresh evidence"),
        call("verify_recovery"),
        call("close_incident", summary="Cleared corrupted cache entries and verified recovery.",
             evidence_ids=["EV-0002"], expected_world_version=3, reason="verified recovery"),
    ]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-f", decisions)
    outcome = controller.run()
    evs = events(trace)
    stale_at = next(i for i, (e, _) in enumerate(evs) if e == "stale_precondition")
    after = evs[stale_at:]
    assert any(e == "tool_result" and p["origin"] == "controller" and p["call"]["name"] == "get_service_health"
               for e, p in after)
    assert any(e == "plan_revision" and p["trigger"] == TRIGGER_STALE for e, p in after)
    clears = [args["expected_world_version"] for name, args in registry.executed if name == "clear_cache"]
    assert clears == [1, 2]  # the stale call was not retried with a silently patched version
    assert outcome.status == "resolved"


@pytest.mark.student
def test_known_stale_version_is_caught_by_controller_before_the_simulator(tmp_path):
    decisions = [
        call("get_metrics", service="redis-cache"),
        call("clear_cache", service="redis-cache", expected_world_version=1, reason="checksum mismatches in redis logs"),
        call("restart_service", service="cart-service", expected_world_version=1, reason="stale session state"),
    ]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    controller.run()
    assert "restart_service" not in registry.names()
    stale = next(p for e, p in events(trace) if e == "stale_precondition")
    assert stale["source"] == "controller_precheck" and stale["expected"] == 1 and stale["actual"] == 2


# --------------------------------------------------------------------------- loops (R7)
@pytest.mark.student
def test_identical_observation_is_blocked_after_the_repeat_limit(tmp_path):
    decisions = [call("get_metrics", service="redis-cache")] * 3
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    controller.run()
    assert registry.names().count("get_metrics") == 2
    assert "loop_detected" in names(trace)


# --------------------------------------------------------------------------- closure (R6)
@pytest.mark.student
def test_premature_close_is_rejected_and_success_is_never_claimed(tmp_path):
    decisions = [call("close_incident", summary="Everything looks fine now, closing the incident.",
                      evidence_ids=["EV-0001"], expected_world_version=1, reason="looks resolved")]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    outcome = controller.run()
    assert "close_incident" not in registry.names()
    assert any(e == "policy_rejection" and p["code"] == "premature_close" for e, p in events(trace))
    assert outcome.status != "resolved"


@pytest.mark.student
def test_happy_path_closes_with_verify_evidence_and_drops_invented_ids(tmp_path):
    decisions = [
        call("get_metrics", service="redis-cache"),
        call("get_logs", service="redis-cache", limit=10),
        call("clear_cache", service="redis-cache", expected_world_version=1, reason="checksum mismatches in redis logs"),
        call("verify_recovery"),
        call("close_incident", summary="Cleared corrupted cache entries; verify_recovery passed.",
             evidence_ids=["EV-9999"], expected_world_version=2, reason="verified recovery"),
    ]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    outcome = controller.run()
    assert outcome.status == "resolved"
    verify_ev = tool_results(trace, "verify_recovery")[-1]["result"]["evidence_id"]
    close = tool_results(trace, "close_incident")[-1]
    assert close["result"]["status"] == "ok"
    assert close["call"]["arguments"]["evidence_ids"] == [verify_ev]
    assert names(trace)[-1] == "terminal_result"


@pytest.mark.student
def test_failed_verification_after_remediation_triggers_replan(tmp_path):
    decisions = [
        call("get_metrics", service="cart-service"),
        call("restart_service", service="cart-service", expected_world_version=1, reason="cart sessions inconsistent"),
        call("verify_recovery"),
    ]
    controller, _, trace = build(tmp_path, "TEST-001", "public-a", decisions)
    outcome = controller.run()
    evs = events(trace)
    assert any(e == "replan_trigger" and p["trigger"] == TRIGGER_VERIFICATION_FAILED for e, p in evs)
    assert controller.state.plan.revision >= 1
    assert outcome.status != "resolved"


# --------------------------------------------------------------------------- budgets (R12)
@pytest.mark.student
def test_llm_budget_pressure_escalates_safely_before_the_hard_limit(tmp_path):
    decisions = [call("get_metrics", service=s) for s in ("redis-cache", "cart-service", "checkout-service", "payment-service")]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions,
                                        budget=BudgetManager(max_llm_calls=4, max_tool_calls=28))
    outcome = controller.run()
    assert outcome.status == "escalated"
    assert outcome.llm_calls <= 4
    assert "budget_warning" in names(trace)
    escalation = tool_results(trace, "escalate_incident")[-1]
    assert escalation["origin"] == "controller" and escalation["result"]["status"] == "ok"
    assert escalation["call"]["arguments"]["evidence_ids"]


@pytest.mark.student
def test_llm_budget_exhaustion_after_remediation_verifies_then_closes(tmp_path):
    decisions = [
        call("clear_cache", service="redis-cache", expected_world_version=1, reason="cache corruption suspected from ticket"),
        ModelReply(content="I think it is fixed."),
    ]
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", decisions,
                                        budget=BudgetManager(max_llm_calls=4, max_tool_calls=28))
    outcome = controller.run()
    assert outcome.status == "resolved"
    assert outcome.llm_calls == 4
    verify = tool_results(trace, "verify_recovery")[-1]
    close = tool_results(trace, "close_incident")[-1]
    assert verify["origin"] == "controller" and verify["result"]["data"]["criteria_met"] is True
    assert verify["result"]["evidence_id"] in close["call"]["arguments"]["evidence_ids"]


@pytest.mark.student
def test_tool_budget_exhaustion_reports_budget_exhausted(tmp_path):
    controller, registry, trace = build(tmp_path, "TEST-001", "public-a", [call("verify_recovery")],
                                        budget=BudgetManager(max_llm_calls=14, max_tool_calls=1))
    outcome = controller.run()
    assert outcome.status == "budget_exhausted"
    assert outcome.tool_calls == 1
    assert any(e == "budget_exhaustion" and p["resource"] == "tools" for e, p in events(trace))


@pytest.mark.student
def test_runtime_budget_is_enforced_with_an_injected_clock(tmp_path):
    clock = FakeClock()

    class SlowModel(ScriptedModelClient):
        def decide(self, messages, tools):
            clock.now += 50.0
            return super().decide(messages, tools)

    decisions = [call("get_metrics", service=s) for s in ("redis-cache", "cart-service", "checkout-service", "payment-service")]
    controller, _, trace = build(tmp_path, "TEST-001", "public-a", [], model=SlowModel(decisions, [PLAN]), clock=clock)
    outcome = controller.run()
    assert outcome.status == "budget_exhausted"
    assert any(e == "budget_exhaustion" and p["resource"] == "runtime" for e, p in events(trace))


@pytest.mark.student
def test_human_approval_wait_is_excluded_from_the_runtime_budget(tmp_path):
    clock = FakeClock()

    class SlowHuman(ApprovalGateway):
        def approve(self, action, arguments, justification):
            clock.now += 1000.0
            return True

    decisions = [
        call("get_metrics", service="order-db"),
        call("failover_database", service="order-db", expected_world_version=1, reason="primary saturated: db_conn and p95 high"),
        call("verify_recovery"),
        call("close_incident", summary="Failed over the degraded order-db primary; recovery verified.",
             evidence_ids=["EV-0002"], expected_world_version=2, reason="verified recovery"),
    ]
    controller, registry, trace = build(tmp_path, "TEST-003", "public-h", decisions, gateway=SlowHuman(), clock=clock)
    outcome = controller.run()
    assert outcome.status == "resolved"
    terminal = next(p for e, p in events(trace) if e == "terminal_result")
    assert terminal["elapsed_wall_seconds"] >= 1000 and terminal["elapsed_agent_seconds"] < 120


# --------------------------------------------------------------------------- telemetry + impossible
@pytest.mark.student
def test_transient_telemetry_failure_is_retried_boundedly(tmp_path):
    # TEST-001/public-b: the first get_metrics call hits a transient telemetry failure.
    controller, registry, trace = build(tmp_path, "TEST-001", "public-b", [call("get_metrics", service="inventory-service")])
    controller.run()
    metrics = tool_results(trace, "get_metrics")
    assert [m["result"]["status"] for m in metrics[:2]] == ["transient_error", "ok"]
    retry = next(p for e, p in events(trace) if e == "retry" and p["operation"] == "tool:get_metrics")
    assert retry["retry_count"] == 1
    seen_by_model = json.loads(next(m for m in controller.state.messages if m["role"] == "tool")["content"])
    assert seen_by_model["status"] == "ok"


@pytest.mark.student
def test_impossible_external_dependency_incident_is_escalated_not_resolved(tmp_path):
    # TEST-003/hidden-j: failure outside the managed platform; no local remediation can fix it.
    decisions = [
        call("get_metrics", service="payment-service"),
        call("get_logs", service="payment-service", limit=10),
        call("restart_service", service="payment-service", expected_world_version=1, reason="try clearing bad worker state"),
        call("verify_recovery"),
        call("escalate_incident", reason="payment-service errors persist after restart; the failing dependency is external",
             evidence_ids=["EV-0003", "EV-0004"]),
    ]
    controller, _, trace = build(tmp_path, "TEST-003", "hidden-j", decisions)
    outcome = controller.run()
    assert outcome.status == "escalated"
    assert any(e == "replan_trigger" and p["trigger"] == TRIGGER_VERIFICATION_FAILED for e, p in events(trace))
    assert "close_incident" not in [p["call"]["name"] for e, p in events(trace) if e == "tool_result"]


# --------------------------------------------------------------------------- policy + planner units
@pytest.mark.student
def test_replan_policy_distinguishes_trigger_types():
    policy = ReplanPolicy()
    verify_failed = {"status": "ok", "tool": "verify_recovery", "data": {"criteria_met": False}}
    assert policy.classify({"status": "stale_precondition"}) == TRIGGER_STALE
    assert policy.classify({"status": "approval_denied"}) == TRIGGER_APPROVAL_DENIED
    assert policy.classify({"status": "error", "tool": "rollback_deployment", "retryable": False}) == TRIGGER_ACTION_FAILED
    assert policy.classify(verify_failed, after_remediation=True) == TRIGGER_VERIFICATION_FAILED
    assert policy.classify(verify_failed, after_remediation=False) is None  # baseline check before any fix
    assert policy.classify({"status": "transient_error", "tool": "get_metrics", "retryable": True}) is None
    assert policy.classify({"status": "error", "tool": "get_logs", "retryable": False}) is None
    assert policy.should_replan({"status": "loop_blocked"}) is True


@pytest.mark.student
def test_planner_revision_increments_and_falls_back_on_invalid_output():
    with pytest.raises(PlanOutputError):
        Planner(ScriptedModelClient(structured_outputs=[{"steps": []}])).create({"status": "ok"})
    invalid = Planner(ScriptedModelClient(structured_outputs=[{"hypothesis": "", "rationale_summary": "", "steps": []}]))
    current = invalid.fallback_plan({"data": {"suspected_service": "checkout-service"}})
    current.steps[0].status = "done"
    revised = invalid.revise(current, {"trigger": TRIGGER_APPROVAL_DENIED, "tool": "rollback_deployment",
                                       "service": "checkout-service"}, "state summary")
    assert revised.revision == current.revision + 1 and invalid.last_source == "deterministic"
    assert revised.steps[0].step_id == current.steps[0].step_id and revised.steps[0].status == "done"
    pending = [s.objective for s in revised.steps if s.status == "pending"]
    assert any("verify_recovery" in text for text in pending) and any("close" in text for text in pending)
    valid = Planner(ScriptedModelClient(structured_outputs=[PLAN]))
    from_model = valid.revise(revised, {"status": "stale_precondition"}, "state summary")
    assert valid.last_source == "model" and from_model.revision == revised.revision + 1
    assert from_model.hypothesis == PLAN["hypothesis"]
