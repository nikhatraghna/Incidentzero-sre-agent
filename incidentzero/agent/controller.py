from __future__ import annotations

import json
import re
import time
import traceback
from collections.abc import Callable
from typing import Any, TypeVar

from incidentzero.approval.gateway import ApprovalGateway
from incidentzero.domain.models import AgentOutcome, AgentPlan, ModelReply, ToolCall
from incidentzero.model.base import ModelClient
from incidentzero.model.errors import PermanentModelError, TransientModelError
from incidentzero.telemetry.budget import BudgetExceeded, BudgetManager, RuntimeBudget, RuntimeBudgetExceeded
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry

from .config import load_limits, load_services
from .planner import Planner, plan_to_dict
from .policies import (
    MUTATING_TOOLS, OBSERVATION_TOOLS, REMEDIATION_TOOLS, SYMPTOM_TOOLS, TERMINAL_TOOLS, TRIGGER_APPROVAL_DENIED,
    TRIGGER_ACTION_FAILED, TRIGGER_BUDGET, TRIGGER_CONTRADICTION, TRIGGER_LOOP, TRIGGER_PRIORITY, TRIGGER_STALE,
    TRIGGER_VERIFICATION_FAILED, VERSIONED_TOOLS, LoopGuard, RecoveryObjective, ReplanPolicy, RiskPolicy,
    observed_health,
)
from .prompts import SYSTEM_PROMPT, USER_GOAL
from .recovery import RetryPolicy
from .state import AgentState

T = TypeVar("T")

TERMINAL_STATUSES = frozenset({"resolved", "escalated", "aborted", "budget_exhausted", "failed"})
VALIDATION_CODES = frozenset({"unknown_tool", "malformed_arguments", "schema_violation", "unknown_service"})
_REMEDIATION_WORDS = {
    "restart_service": ("restart",),
    "scale_service": ("scale", "replica", "capacity"),
    "clear_cache": ("cache", "invalidat"),
    "rollback_deployment": ("rollback", "roll back"),
    "failover_database": ("failover", "fail over"),
    "shift_traffic": ("shift", "traffic"),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _short(exc: BaseException, limit: int = 240) -> str:
    return f"{type(exc).__name__}: {str(exc)[:limit]}"


def _is_verify_step(text: str) -> bool:
    return "verify_recovery" in text or ("verif" in text and "recover" in text)


class AgentController:
    """Reliable bounded controller.

    The model proposes; Python validates, authorizes (human approval for high/critical risk),
    executes through the ToolRegistry, records every decision in the trace, and decides when
    to retry, re-plan, abort, verify, close or escalate. All limits come from
    ``configs/limits.json`` and all risk levels from ``configs/risk_policy.json``.
    """

    def __init__(
        self,
        model: ModelClient,
        tools: ToolRegistry,
        approval: ApprovalGateway,
        budget: BudgetManager,
        trace: TraceRecorder,
        *,
        limits: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.tools = tools
        self.approval = approval
        self.budget = budget
        self.trace = trace
        self.limits = dict(limits) if limits is not None else load_limits()
        self.state = AgentState()
        self.planner = Planner(model)
        self.risk = RiskPolicy()
        self.replan_policy = ReplanPolicy()
        self.loop_guard = LoopGuard(int(self.limits["max_same_action_repeats"]))
        runtime_limit = getattr(budget, "max_runtime_seconds", None) or self.limits["max_runtime_seconds"]
        self.runtime = RuntimeBudget(float(runtime_limit), clock=clock)
        self.retry_policy = RetryPolicy(
            max_attempts=int(self.limits["max_consecutive_model_retries"]),
            sleeper=sleeper,
            time_remaining=lambda: self.runtime.remaining,
        )
        self.warning_calls = int(self.limits["warning_llm_calls_remaining"])
        self.max_unproductive_turns = int(self.limits["max_same_action_repeats"])
        self._runtime_warning_fraction = min(0.5, self.warning_calls / max(1, int(self.limits["max_llm_calls"])))
        try:
            services, critical = load_services()
            self.known_services: frozenset[str] | None = frozenset(services)
            self.critical_path: tuple[str, ...] = tuple(critical)
        except Exception:  # configs missing: fall back to the simulator's own "Unknown service" errors
            self.known_services, self.critical_path = None, ()
        self.objective = RecoveryObjective()
        # run-scoped bookkeeping
        self._outcome: AgentOutcome | None = None
        self._notes: list[str] = []
        self._pending_triggers: list[dict[str, Any]] = []
        self._replan_keys: set[str] = set()
        self._turn = 0
        self._call_seq = 0
        self._seen_call_ids: set[str] = set()
        self._contradiction_revision: int | None = None
        self._version_before_call: int | None = None
        self._last_verify_after_remediation = False
        self._tool_name_set: frozenset[str] = frozenset()
        self._metrics: dict[str, Any] = {
            "model_requests": {}, "model_retries": 0, "tool_retries": 0, "validation_failures": 0,
            "policy_rejections": 0, "loop_blocks": 0, "plan_revisions": 0, "stale_preconditions": 0,
            "approvals_requested": 0, "approvals_granted": 0, "approvals_denied": 0,
            "high_risk_proposed": 0, "high_risk_executed": 0, "controller_tool_calls": 0,
            "approval_wait_seconds": 0.0, "unproductive_turns": 0,
        }

    # =================================================================== public entry point
    def run(self) -> AgentOutcome:
        self.runtime.start()
        self.state.status = "running"
        self._tool_name_set = frozenset(t["function"]["name"] for t in self.tools.groq_tools)
        self.state.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_GOAL},
        ]
        self.trace.record("run_started", {
            "limits": self.limits,
            "budget": {"max_llm_calls": self.budget.max_llm_calls, "max_tool_calls": self.budget.max_tool_calls,
                       "max_runtime_seconds": self.runtime.max_seconds},
            "risk_policy": dict(self.risk.mapping),
            "model_adapter": type(self.model).__name__,
            "approval_gateway": type(self.approval).__name__,
        })
        try:
            return self._run_loop()
        except RuntimeBudgetExceeded as exc:
            self._record_budget_exhaustion("runtime", str(exc))
            return self._terminate("budget_exhausted", f"{exc}; stopped without a terminal action (incident left open).")
        except BudgetExceeded as exc:
            return self._on_budget_exceeded(exc)
        except PermanentModelError as exc:
            return self._terminate(
                "aborted",
                f"Aborted on a permanent model/configuration error ({_short(exc)}). No further actions were taken; "
                "the incident remains open for a human.")
        except TransientModelError as exc:
            attempts = getattr(self.retry_policy, "max_attempts", "?")
            return self._terminate(
                "aborted",
                f"Aborted: the model provider stayed unavailable after {attempts} bounded attempts ({_short(exc)}). "
                "No further actions were taken; the incident remains open for a human.")
        except Exception as exc:  # never crash the caller; never claim success
            self.trace.record("internal_error", {"error": _short(exc, 500), "traceback": traceback.format_exc()[-3000:]})
            return self._terminate("failed", f"Controller stopped on an unexpected error ({_short(exc)}); no success is claimed.")

    def _run_loop(self) -> AgentOutcome:
        incident = self._bootstrap()
        if incident.get("status") != "ok":
            return self._terminate("failed", "Could not read the incident ticket (get_incident did not succeed); no plan can be grounded.")
        self._create_initial_plan(incident)
        while True:
            if self._outcome is not None:
                return self._outcome
            self.runtime.check()
            outcome = self._budget_gate()
            if outcome is not None:
                return outcome
            reply = self._model_decide()
            outcome = self._handle_reply(reply)
            if outcome is not None:
                return outcome

    # =================================================================== bootstrap + planning
    def _bootstrap(self) -> dict[str, Any]:
        result = self._run_tool("get_incident", {}, origin="controller", purpose="bootstrap: read the incident ticket")
        self.trace.record("bootstrap_incident", result)
        if result.get("status") == "ok":
            self.state.incident = result.get("data") or {}
            self.objective = RecoveryObjective.from_incident(self.state.incident)
        self.state.messages.append({"role": "system", "content": f"Current incident evidence: {_json(result)}"})
        return result

    def _create_initial_plan(self, incident: dict[str, Any]) -> None:
        context = {
            "budget": {"llm_calls": self.budget.max_llm_calls, "tool_calls": self.budget.max_tool_calls,
                       "runtime_seconds": self.runtime.max_seconds},
            "services": sorted(self.known_services) if self.known_services else None,
        }
        source = "model"
        plan: Any = None
        try:
            plan = self._invoke_model("plan_create", lambda: self.planner.create(incident, context))
        except (PermanentModelError, BudgetExceeded):
            raise
        except TransientModelError as exc:
            if getattr(exc, "kind", None) != "invalid_output":
                raise  # provider unavailable after bounded retries -> abort
            plan = None
            self._validation_failure("plan_create", "invalid_structured_output", _short(exc))
        except Exception as exc:  # invalid plan JSON/schema or an adapter that cannot produce one
            plan = None
            self._validation_failure("plan_create", "invalid_plan", _short(exc))
        if not isinstance(plan, AgentPlan):
            plan, source = self._fallback_plan(incident), "deterministic_fallback"
        plan = Planner.ensure_terminal_steps(plan)
        self.state.plan = plan
        self.trace.record("plan_created", {"source": source, "plan": plan_to_dict(plan)})
        self.state.messages.append({"role": "system", "content": self._plan_note(plan, source)})

    def _fallback_plan(self, incident: dict[str, Any]) -> AgentPlan:
        maker = getattr(self.planner, "fallback_plan", None)
        if callable(maker):
            try:
                plan = maker(incident)
                if isinstance(plan, AgentPlan):
                    return plan
            except Exception:
                pass
        return Planner(self.model).fallback_plan(incident)

    # =================================================================== model calls
    def _invoke_model(self, operation: str, fn: Callable[[], T]) -> T:
        """One logical model operation with bounded retries. Every attempt is a real request:
        it is charged to the LLM budget and traced before it is sent."""
        attempts = {"n": 0}

        def attempt() -> T:
            attempts["n"] += 1
            n = attempts["n"]
            self.runtime.check()
            if n > 1:
                self._metrics["model_retries"] += 1
                self.trace.record("retry", {"operation": operation, "attempt": n, "retry_count": n - 1,
                                            "max_attempts": getattr(self.retry_policy, "max_attempts", None)})
            self.budget.consume_llm()
            counts = self._metrics["model_requests"]
            counts[operation] = counts.get(operation, 0) + 1
            self.trace.record("model_request", {"operation": operation, "attempt": n,
                                                "llm_calls_used": self.budget.llm_calls,
                                                "remaining_llm": self.budget.remaining_llm,
                                                "messages": len(self.state.messages)})
            try:
                return fn()
            except TransientModelError as exc:
                self.trace.record("model_error", {"operation": operation, "attempt": n, "kind": "transient",
                                                  "subkind": getattr(exc, "kind", None), "error": _short(exc, 400),
                                                  "retry_after": getattr(exc, "retry_after", None)})
                raise
            except PermanentModelError as exc:
                self.trace.record("model_error", {"operation": operation, "attempt": n, "kind": "permanent",
                                                  "error": _short(exc, 400)})
                raise

        return self.retry_policy.call_model(attempt)

    def _model_decide(self) -> ModelReply:
        tools = self._tools_for_turn()
        return self._invoke_model("decide", lambda: self.model.decide(self.state.messages, tools))

    def _tools_for_turn(self) -> list[dict[str, Any]]:
        if not self.state.wrap_up:
            return self.tools.groq_tools
        allowed = self._allowed_tools()
        return [t for t in self.tools.groq_tools if t["function"]["name"] in allowed]

    def _allowed_tools(self) -> set[str]:
        """Tools still affordable during budget wrap-up (R5: verify/close/escalate first)."""
        allowed = set(TERMINAL_TOOLS)
        if self.budget.remaining_tools >= 2:
            allowed.add("verify_recovery")
        if self.budget.remaining_tools >= 3:
            allowed |= set(REMEDIATION_TOOLS)
        return allowed

    # =================================================================== one model turn
    def _handle_reply(self, reply: Any) -> AgentOutcome | None:
        self._turn += 1
        reply = self._coerce_reply(reply)
        calls = self._normalise_calls(reply.tool_calls)
        usage = reply.usage if isinstance(reply.usage, dict) else {}
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.state.token_usage[key] = self.state.token_usage.get(key, 0) + int(value)
        self.trace.record("model_reply", {
            "turn": self._turn, "content": (reply.content or "")[:800], "finish_reason": reply.finish_reason,
            "usage": usage, "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
        })
        self._append_assistant(reply, calls)
        if not calls:
            return self._handle_no_tool_call(reply)

        primary, extras = calls[0], calls[1:]
        self.trace.record("tool_proposal", {"turn": self._turn, "name": primary.name, "arguments": primary.arguments})
        result = self._handle_proposal(primary)
        self._append_tool_result(primary, result)
        for extra in extras:
            self.trace.record("tool_proposal_skipped", {"turn": self._turn, "name": extra.name, "arguments": extra.arguments})
            self._append_tool_result(extra, {
                "status": "not_executed", "tool": str(extra.name), "world_version": self.state.latest_world_version,
                "evidence_id": None, "data": None, "retryable": True,
                "message": "Only one tool call per turn is executed; re-propose this call next turn if still needed.",
            })
        if self._outcome is not None:
            return self._outcome
        self._process_triggers()
        if self.state.unproductive_streak > self.max_unproductive_turns:
            if self.state.wrap_up:
                return self._safe_finish("the model made no executable progress during budget wrap-up", exhausted=None)
            return self._terminate(
                "failed",
                f"The model made no executable progress on {self.state.unproductive_streak} consecutive turns "
                "(rejected, blocked or missing tool calls); the controller stopped without claiming success.")
        self._flush_notes()
        return None

    def _handle_no_tool_call(self, reply: ModelReply) -> AgentOutcome | None:
        self.state.unproductive_streak += 1
        self._metrics["unproductive_turns"] += 1
        provider_rejected = reply.finish_reason == "tool_use_failed"
        self.trace.record("unproductive_turn", {
            "turn": self._turn, "reason": "provider_rejected_tool_call" if provider_rejected else "no_tool_call",
            "streak": self.state.unproductive_streak, "content": (reply.content or "")[:400],
        })
        if provider_rejected:
            self._validation_failure("decide", "provider_rejected_tool_call", (reply.content or "")[:300])
        if self.state.unproductive_streak > self.max_unproductive_turns:
            if self.state.wrap_up:
                return self._safe_finish("the model made no executable progress during budget wrap-up", exhausted=None)
            return self._terminate(
                "failed",
                f"The model produced no executable tool call on {self.state.unproductive_streak} consecutive turns. "
                "Natural-language output is not evidence of recovery, so the controller stopped without claiming "
                "success (incident left open).")
        if provider_rejected:
            self._note("[controller] Your last tool call was rejected as malformed by the model provider and was NOT "
                       "executed. Re-issue exactly one valid tool call that matches the declared schema.")
        else:
            self._note(f"[controller] No tool call was made. Text is not evidence: the incident is still open "
                       f"(world_version={self.state.latest_world_version}). Continue with exactly one tool call "
                       "(investigate, remediate, verify_recovery, close_incident citing verify evidence, or escalate_incident).")
        self._process_triggers()
        self._flush_notes()
        return None

    # =================================================================== validation boundary (R8)
    def _handle_proposal(self, call: ToolCall) -> dict[str, Any]:
        name, args = call.name, call.arguments
        # 1. known tool name
        if not isinstance(name, str) or name not in self._tool_name_set:
            return self._reject(call, "unknown_tool",
                                f"Unknown tool {name!r}. Use one of: {', '.join(sorted(self._tool_name_set))}.")
        # 2. JSON/dictionary shape
        if not isinstance(args, dict):
            return self._reject(call, "malformed_arguments", "Tool arguments must be a JSON object.")
        if "__malformed_arguments__" in args:
            raw = str(args.get("__malformed_arguments__"))[:200]
            return self._reject(call, "malformed_arguments",
                                f"Your arguments were not valid JSON ({raw!r}). Re-issue the call with a valid JSON object.")
        # 3. schema: required fields, types, enums and ranges
        ok, error = self.tools.validate(name, args)
        if not ok:
            return self._reject(call, "schema_violation", f"Arguments do not match the {name} schema: {error}")
        # 4. permitted values the schema cannot express
        service = args.get("service")
        if isinstance(service, str) and self.known_services is not None and service not in self.known_services:
            return self._reject(call, "unknown_service",
                                f"Unknown service {service!r}. Valid services: {', '.join(sorted(self.known_services))}.")
        self.state.repeated_actions[name] = self.state.repeated_actions.get(name, 0) + 1
        if name in MUTATING_TOOLS and self.risk.requires_human_approval(name):
            self._metrics["high_risk_proposed"] += 1
        # 5. budget policy
        problem = self._budget_policy_violation(name)
        if problem:
            return self._reject(call, "budget_policy", problem)
        # 6. loop detection (observations are keyed to the current world version)
        if self.loop_guard.record(name, self._loop_arguments(name, args)):
            return self._block_loop(call, "exact_repeat")
        if name in REMEDIATION_TOOLS and self.loop_guard.would_exceed_semantic(name, args):
            return self._block_loop(call, "same_remediation_repeated")
        # 7. state preconditions: world version, stale re-observation, proof before closure
        blocked = self._check_preconditions(call)
        if blocked is not None:
            return blocked
        # 8. evidence required before remediation (rollback target, high/critical risk)
        if name in REMEDIATION_TOOLS:
            blocked = self._ensure_action_evidence(call)
            if blocked is not None:
                return blocked
        # 9. human approval (inside _run_tool) + execution
        result = self._run_tool(name, call.arguments, origin="model", model_reason=call.arguments.get("reason"))
        self._after_model_tool(call, result)
        return result

    def _loop_arguments(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in OBSERVATION_TOOLS:
            return {**args, "@world_version": self.state.latest_world_version}
        return args

    def _budget_policy_violation(self, name: str) -> str | None:
        remaining = self.budget.remaining_tools
        need = 1 if name in TERMINAL_TOOLS else 2 if name in OBSERVATION_TOOLS else 3
        if remaining < need:
            return (f"Only {remaining} tool call(s) remain; {name} needs {need} including the reserve for "
                    "verification/closure or escalation. Close with verified evidence or escalate_incident now.")
        if self.state.wrap_up and name not in self._allowed_tools():
            return (f"Budget wrap-up: only {sorted(self._allowed_tools())} are allowed now "
                    f"({self.budget.remaining_llm} model call(s), {remaining} tool call(s) left).")
        return None

    def _check_preconditions(self, call: ToolCall) -> dict[str, Any] | None:
        name, args = call.name, call.arguments
        latest = self.state.latest_world_version
        if name in VERSIONED_TOOLS:
            expected = args.get("expected_world_version")
            if isinstance(latest, int) and isinstance(expected, (int, float)):
                if expected < latest:
                    # Known-stale version: never forward it and never silently swap the number.
                    self.state.unproductive_streak += 1
                    result = self._stale_result(name, expected, latest)
                    self._handle_stale(name, args, result, source="controller_precheck", external=False)
                    return result
                if expected > latest:
                    return self._reject(call, "unobserved_world_version",
                                        f"expected_world_version={expected} has never been observed; the newest observed "
                                        f"world_version is {latest}. Use only observed versions.")
            if self.state.reobserve_required:
                return self._reject(call, "reobserve_required",
                                    "The world changed (stale precondition) and no fresh observation has succeeded since. "
                                    "Observe the relevant service before acting.")
        if name == "close_incident":
            return self._check_close(call)
        return None

    def _check_close(self, call: ToolCall) -> dict[str, Any] | None:
        verify = self.state.latest_verify
        if verify is None:
            return self._reject(call, "premature_close",
                                "close_incident requires a prior verify_recovery reporting criteria_met=true. Call verify_recovery first.")
        data = verify.get("data") or {}
        if not data.get("criteria_met"):
            return self._reject(call, "premature_close",
                                f"The latest verify_recovery ({verify.get('evidence_id')}) reports criteria_met=false "
                                f"({self._digest(verify)}). Recovery is not proven: remediate further or escalate.")
        proof = self.state.proof_of_recovery()
        if proof is None:
            return self._reject(call, "verification_outdated",
                                f"The world changed after the last verification ({verify.get('evidence_id')} at "
                                f"v{verify.get('world_version')}, now v{self.state.latest_world_version}). Call verify_recovery again.")
        cited = [e for e in call.arguments.get("evidence_ids", []) if isinstance(e, str)]
        observed = set(self.state.evidence_ids)
        kept = [e for e in dict.fromkeys(cited) if e in observed]
        dropped = [e for e in cited if e not in observed]
        added: list[str] = []
        if proof["evidence_id"] not in kept:
            kept.append(proof["evidence_id"])
            added.append(proof["evidence_id"])
        if dropped or added:
            call.arguments = {**call.arguments, "evidence_ids": kept}
            self.trace.record("close_evidence_sanitized", {"cited": cited, "dropped_unobserved": dropped,
                                                            "added_verify_evidence": added, "final": kept})
        return None

    def _ensure_action_evidence(self, call: ToolCall) -> dict[str, Any] | None:
        """R3: high/critical actions must rest on observed evidence about their target, and a
        rollback must target the observed previous release. Missing evidence is gathered by the
        controller (read-only tools) so the human approver sees it."""
        name, args = call.name, call.arguments
        service = args.get("service") if isinstance(args.get("service"), str) else None
        gated = self.risk.requires_human_approval(name)
        needs: list[tuple[str, dict[str, Any]]] = []
        if service and name == "rollback_deployment" and not self.state.latest_for(service, "get_deployments"):
            needs.append(("get_deployments", {"service": service}))
        if service and gated and not self.state.has_evidence_for(service, SYMPTOM_TOOLS):
            needs.append(("get_metrics", {"service": service}))
        if needs:
            if self.budget.remaining_tools < len(needs) + 3:
                return self._reject(call, "budget_policy",
                                    f"{name} needs evidence gathering plus execution, verification and closure, which the "
                                    f"remaining {self.budget.remaining_tools} tool call(s) cannot cover. Escalate instead.")
            expected = args.get("expected_world_version")
            gathered: list[dict[str, Any]] = []
            for tool, targs in needs:
                result = self._run_tool(tool, targs, origin="controller", purpose=f"evidence required before {name}")
                if result.get("status") != "ok" and tool == "get_metrics" and self.budget.remaining_tools > 3:
                    result = self._run_tool("get_service_health", targs, origin="controller",
                                            purpose=f"alternative evidence path before {name}")
                self.trace.record("evidence_gathered", {"for_action": name, "tool": result.get("tool", tool),
                                                        "service": service, "status": result.get("status"),
                                                        "evidence_id": result.get("evidence_id")})
                if result.get("status") == "ok":
                    self._after_observation(str(result.get("tool", tool)), targs, result)
                gathered.append(result)
            ok_rows = [r for r in gathered if r.get("status") == "ok"]
            if ok_rows:
                self._note(f"[controller] Before {name} the controller gathered required evidence: "
                           + "; ".join(f"{r.get('tool')} {r.get('evidence_id')}@v{r.get('world_version')}: {self._digest(r)}" for r in ok_rows))
            latest = self.state.latest_world_version
            if isinstance(expected, (int, float)) and isinstance(latest, int) and latest != expected:
                result = self._stale_result(name, expected, latest)
                self._handle_stale(name, args, result, source="world_changed_during_evidence_gathering", external=True)
                return result
            if name == "rollback_deployment" and service and not self.state.latest_for(service, "get_deployments"):
                return self._reject(call, "evidence_unavailable",
                                    "Deployment history for the rollback target could not be observed; not rolling back blind.")
            if gated and service and not self.state.has_evidence_for(service, SYMPTOM_TOOLS):
                return self._reject(call, "evidence_unavailable",
                                    f"No health/metrics/log evidence about {service} could be observed; a {name} cannot be "
                                    "justified to the approver yet.")
        if name == "rollback_deployment" and service:
            deployments = self.state.latest_for(service, "get_deployments") or {}
            previous = (((deployments.get("data") or {}).get("previous") or {}).get("version"))
            if previous is not None and args.get("target_version") != previous:
                return self._reject(call, "invalid_target_version",
                                    f"target_version {args.get('target_version')!r} is not the known previous release of "
                                    f"{service}: get_deployments ({deployments.get('evidence_id')}) reports previous={previous!r}.")
        return None

    # =================================================================== execution + approval
    def _run_tool(self, name: str, args: dict[str, Any], *, origin: str, purpose: str | None = None,
                  model_reason: Any = None) -> dict[str, Any]:
        """Single execution path for every tool call (model- or controller-initiated):
        policy-driven human approval, budget accounting, execution, trace, bounded retry of
        transient telemetry failures for read-only tools."""
        if self.risk.requires_human_approval(name):
            approved, denial = self._request_approval(name, args, origin, purpose, model_reason)
            if not approved:
                return denial or {}
        result = self._execute(name, args, origin)
        attempts = 1
        max_attempts = max(1, int(self.limits["max_consecutive_model_retries"]))
        while (result.get("status") == "transient_error" and result.get("retryable") and name in OBSERVATION_TOOLS
               and attempts < max_attempts and self.budget.remaining_tools > 1):
            attempts += 1
            failed = result
            self._metrics["tool_retries"] += 1
            self.trace.record("retry", {"operation": f"tool:{name}", "attempt": attempts, "retry_count": attempts - 1,
                                        "max_attempts": max_attempts, "previous_error": failed.get("message"),
                                        "previous_evidence_id": failed.get("evidence_id")})
            result = self._execute(name, args, origin)
            if result.get("status") == "ok":
                result = {**result, "controller_note": f"Recovered from a transient telemetry error "
                                                       f"({failed.get('evidence_id')}) with a bounded retry."}
        if result.get("status") == "transient_error" and name in OBSERVATION_TOOLS:
            result = {**result, "controller_note": "Still failing after bounded retries; use another observation path "
                                                   "(e.g. get_service_health instead of get_metrics)."}
        return result

    def _execute(self, name: str, args: dict[str, Any], origin: str) -> dict[str, Any]:
        self.runtime.check()
        self.budget.consume_tool()
        if origin != "model":
            self._metrics["controller_tool_calls"] += 1
        self._version_before_call = self.state.latest_world_version
        if name == "verify_recovery":
            self._last_verify_after_remediation = self.state.mutations_since_verify > 0
        result = self.tools.execute(name, args)
        if not isinstance(result, dict):
            result = {"status": "error", "tool": name, "world_version": self.state.latest_world_version,
                      "evidence_id": None, "data": None, "retryable": False,
                      "message": f"Tool returned a non-dict result ({type(result).__name__})."}
        self.state.observe_result(result)
        if result.get("status") == "ok" and name in OBSERVATION_TOOLS:
            self.state.record_observation(name, args, result)
        if result.get("status") == "ok" and name in MUTATING_TOOLS and self.risk.requires_human_approval(name):
            self._metrics["high_risk_executed"] += 1
        self.trace.record("tool_result", {
            "origin": origin, "call": {"name": name, "arguments": args}, "result": result,
            "world_version_before": self._version_before_call, "llm_calls_used": self.budget.llm_calls,
            "tool_calls_used": self.budget.tool_calls, "remaining_tools": self.budget.remaining_tools,
        })
        return result

    def _request_approval(self, name: str, args: dict[str, Any], origin: str, purpose: str | None,
                          model_reason: Any) -> tuple[bool, dict[str, Any] | None]:
        key = self.loop_guard.semantic_key(name, args)
        prior = self.state.denied_actions.get(key)
        if prior is not None:
            if prior["count"] >= self.loop_guard.max_same_action_repeats:
                return False, self._policy_block(name, "previously_denied",
                                                 f"{name} was denied by the human approver {prior['count']} times; it will not "
                                                 "be requested again. Choose a different action or escalate.")
            if len(self.state.observations) <= prior["observations_at_denial"]:
                return False, self._policy_block(name, "previously_denied",
                                                 f"The human approver denied {name} and no new evidence has been gathered since. "
                                                 "Choose an alternative or escalate (gather new evidence first if the denial "
                                                 "should be revisited).")
        risk = self.risk.risk(name).value
        justification = self._justification(name, args, origin, purpose, model_reason)
        self._metrics["approvals_requested"] += 1
        self.trace.record("approval_request", {"action": name, "arguments": args, "risk": risk, "origin": origin,
                                               "world_version": self.state.latest_world_version,
                                               "justification": justification})
        started = self.runtime.clock()
        error = None
        try:
            approved = self.approval.approve(name, dict(args), justification) is True
        except Exception as exc:  # e.g. EOF on the console: fail safe means deny
            approved, error = False, _short(exc)
        waited = max(0.0, self.runtime.clock() - started)
        self.runtime.exclude(waited)
        self._metrics["approval_wait_seconds"] += waited
        record = {"action": name, "arguments": args, "risk": risk, "origin": origin, "approved": approved,
                  "wait_seconds": round(waited, 3), "error": error}
        self.state.approvals.append(record)
        self.trace.record("approval_result", record)
        if approved:
            self._metrics["approvals_granted"] += 1
            return True, None
        self._metrics["approvals_denied"] += 1
        info = self.state.denied_actions.setdefault(
            key, {"tool": name, "arguments": LoopGuard.semantic_arguments(args), "count": 0})
        info["count"] += 1
        info["observations_at_denial"] = len(self.state.observations)
        info["world_version"] = self.state.latest_world_version
        return False, {
            "status": "approval_denied", "tool": name, "world_version": self.state.latest_world_version,
            "evidence_id": None, "data": None, "retryable": False,
            "message": f"The human approver denied {name}; it was NOT executed. Do not retry it blindly: gather missing "
                       "evidence, choose a safe alternative, or escalate with evidence.",
        }

    def _justification(self, name: str, args: dict[str, Any], origin: str, purpose: str | None,
                       model_reason: Any) -> str:
        risk = self.risk.risk(name).value
        shown = {k: v for k, v in args.items() if k != "reason"}
        lines = [
            f"Action: {name}({_json(shown)})",
            f"Risk level: {risk} (configs/risk_policy.json); current world_version={self.state.latest_world_version}",
            f"Proposed by: {'the model' if origin == 'model' else 'the controller'}" + (f" - {purpose}" if purpose else ""),
        ]
        if model_reason:
            lines.append(f"Stated reason: {str(model_reason)[:300]}")
        if self.state.plan is not None:
            lines.append(f"Active hypothesis (plan r{self.state.plan.revision}): {self.state.plan.hypothesis[:300]}")
        service = args.get("service")
        rows = list((self.state.latest_by_service.get(service) or {}).values()) if isinstance(service, str) else []
        lines.append("Supporting evidence:" if rows else "Supporting evidence: none observed for the target yet.")
        for row in rows:
            lines.append(f"  - {row.get('evidence_id')} {row.get('tool')} @v{row.get('world_version')}: {self._digest(row)}")
        if self.state.latest_verify:
            v = self.state.latest_verify
            lines.append(f"  - {v.get('evidence_id')} verify_recovery @v{v.get('world_version')}: {self._digest(v)}")
        lines.append("After execution the controller requires verify_recovery (criteria_met=true) before any closure.")
        return "\n".join(lines)

    # =================================================================== after execution
    def _after_model_tool(self, call: ToolCall, result: dict[str, Any]) -> None:
        name, args = call.name, call.arguments
        status = result.get("status")
        if status in ("ok", "error", "stale_precondition", "transient_error", "approval_denied"):
            self.state.unproductive_streak = 0
        if status == "approval_denied":
            self._queue_trigger(TRIGGER_APPROVAL_DENIED, tool=name, service=args.get("service"),
                                details={"arguments": LoopGuard.semantic_arguments(args)})
            return
        if status == "stale_precondition":
            external = (isinstance(result.get("actual"), int) and isinstance(self._version_before_call, int)
                        and result["actual"] > self._version_before_call)
            self._handle_stale(name, args, result, source="simulator", external=external)
            return
        if status == "ok":
            if name in REMEDIATION_TOOLS:
                self.state.remediations.append({"tool": name, "arguments": LoopGuard.semantic_arguments(args),
                                                "evidence_id": result.get("evidence_id"),
                                                "world_version": result.get("world_version"),
                                                "effect": (result.get("data") or {}).get("effect")})
                self.state.mutations_since_verify += 1
                self.loop_guard.note_executed(name, args)
                self._note(f"[controller] {name} executed ({result.get('evidence_id')}; world_version now "
                           f"{result.get('world_version')}). Call verify_recovery before any closure.")
            elif name == "close_incident":
                self._terminate("resolved",
                                f"Incident closed by the simulator after verify_recovery proved recovery "
                                f"(cited evidence: {', '.join(args.get('evidence_ids', []))}).")
                return
            elif name == "escalate_incident":
                self._terminate("escalated", f"Incident escalated with evidence: {str(args.get('reason'))[:240]}")
                return
            if name in OBSERVATION_TOOLS:
                self._after_observation(name, args, result)
            self._update_plan_steps(name, args, result)
            return
        if status == "transient_error" and name in MUTATING_TOOLS:
            self._note(f"[controller] {name} hit a transient error and was not executed. Re-propose it only if still "
                       "justified (high/critical actions will need fresh approval).")
            return
        trigger = self.replan_policy.classify(result)
        if trigger == TRIGGER_ACTION_FAILED and name in MUTATING_TOOLS:
            self._queue_trigger(TRIGGER_ACTION_FAILED, tool=name, service=args.get("service"),
                                details={"message": result.get("message"), "evidence_id": result.get("evidence_id")})

    def _after_observation(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if result.get("status") != "ok":
            return
        if name == "verify_recovery":
            data = result.get("data") or {}
            if data.get("criteria_met"):
                self._note(f"[controller] verify_recovery {result.get('evidence_id')} proves recovery at world_version "
                           f"{result.get('world_version')}. close_incident may cite {result.get('evidence_id')} with "
                           f"expected_world_version={result.get('world_version')}.")
            elif self.replan_policy.classify(result, after_remediation=self._last_verify_after_remediation):
                last = self.state.remediations[-1] if self.state.remediations else {}
                self._queue_trigger(TRIGGER_VERIFICATION_FAILED, tool=last.get("tool"),
                                    service=(last.get("arguments") or {}).get("service"),
                                    details={"verify": self._digest(result), "evidence_id": result.get("evidence_id")})
        elif name in ("get_service_health", "get_metrics"):
            self._check_contradiction()
        self._update_plan_steps(name, args, result)

    def _handle_stale(self, name: str, args: dict[str, Any], result: dict[str, Any], *, source: str,
                      external: bool) -> None:
        """Stale world: never retry blindly. Re-observe the state the action depended on, then
        trigger a plan revision so the action is re-confirmed (or replaced) with fresh evidence."""
        self.state.stale_events += 1
        self._metrics["stale_preconditions"] += 1
        expected = result.get("expected", args.get("expected_world_version"))
        actual = result.get("actual", result.get("world_version"))
        self.trace.record("stale_precondition", {"source": source, "tool": name, "expected": expected, "actual": actual,
                                                 "external_change": external, "evidence_id": result.get("evidence_id")})
        if external:
            self.loop_guard.reset_semantic()
        self.state.reobserve_required = True
        fresh = self._reobserve(name, args)
        self._queue_trigger(TRIGGER_STALE, tool=name, service=args.get("service"),
                            details={"expected": expected, "actual": actual, "source": source,
                                     "reobservation": fresh.get("evidence_id") if fresh else None})
        note = (f"[controller] Stale world: {name} expected world_version {expected} but the world is at {actual}; "
                "it was NOT executed. ")
        if fresh and fresh.get("status") == "ok":
            note += (f"Fresh observation {fresh.get('tool')} {fresh.get('evidence_id')}@v{fresh.get('world_version')}: "
                     f"{self._digest(fresh)}. Re-confirm the action against this evidence; if it is still justified, "
                     f"re-propose it with expected_world_version={self.state.latest_world_version}. Do not retry blindly.")
        else:
            note += "The re-observation did not succeed; observe the relevant service before acting."
        self._note(note)

    def _reobserve(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        latest = self.state.latest_world_version
        service = args.get("service") if isinstance(args.get("service"), str) else None
        if name == "close_incident" or service is None:
            verify = self.state.latest_verify
            if verify is not None and verify.get("world_version") == latest:
                self.state.reobserve_required = False
                return verify
            tool, targs = "verify_recovery", {}
        else:
            for row in (self.state.latest_by_service.get(service) or {}).values():
                if row.get("tool") in SYMPTOM_TOOLS and row.get("world_version") == latest:
                    self.state.reobserve_required = False
                    return row
            tool, targs = "get_service_health", {"service": service}
        if self.budget.remaining_tools <= 1:
            return None
        result = self._run_tool(tool, targs, origin="controller", purpose=f"re-observe after stale precondition on {name}")
        if result.get("status") != "ok" and service and self.budget.remaining_tools > 1:
            result = self._run_tool("get_metrics", {"service": service}, origin="controller",
                                    purpose=f"alternative re-observation after stale precondition on {name}")
        self.trace.record("reobservation", {"for_action": name, "tool": result.get("tool"), "status": result.get("status"),
                                            "evidence_id": result.get("evidence_id"),
                                            "world_version": result.get("world_version")})
        if result.get("status") == "ok":
            self._after_observation(str(result.get("tool")), targs, result)
        return result

    def _check_contradiction(self) -> None:
        """R4: a plan's suspect observed healthy while another component is observed unhealthy."""
        plan = self.state.plan
        if plan is None or self.state.remediations or self._contradiction_revision == plan.revision:
            return
        if not self.known_services:
            return
        suspects = {s for s in self.known_services
                    if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", plan.hypothesis)}
        if not suspects:
            return
        health: dict[str, bool] = {}
        for service, rows in self.state.latest_by_service.items():
            signals = [observed_health(r, self.objective) for r in rows.values()]
            signals = [s for s in signals if s is not None]
            if signals:
                health[service] = all(signals)
        observed_suspects = {s: health[s] for s in suspects if s in health}
        if not observed_suspects or not all(observed_suspects.values()):
            return
        unhealthy_elsewhere = sorted(s for s, ok in health.items() if s not in suspects and not ok)
        if not unhealthy_elsewhere:
            return
        self._contradiction_revision = plan.revision
        self._queue_trigger(TRIGGER_CONTRADICTION, service=sorted(observed_suspects)[0],
                            details={"suspects_observed_healthy": sorted(observed_suspects),
                                     "unhealthy_elsewhere": unhealthy_elsewhere})
        self._note(f"[controller] Evidence contradicts the plan hypothesis: {sorted(observed_suspects)} look healthy "
                   f"while {unhealthy_elsewhere} are unhealthy. The plan is being revised.")

    # =================================================================== re-planning (R4)
    def _queue_trigger(self, trigger: str, *, tool: Any = None, service: Any = None,
                       details: dict[str, Any] | None = None) -> None:
        self._pending_triggers.append({"trigger": trigger, "tool": tool, "service": service, "details": details or {},
                                       "world_version": self.state.latest_world_version, "turn": self._turn})

    def _process_triggers(self) -> None:
        if not self._pending_triggers or self._outcome is not None:
            self._pending_triggers = []
            return
        order = {name: i for i, name in enumerate(TRIGGER_PRIORITY)}
        triggers = sorted(self._pending_triggers, key=lambda t: order.get(t["trigger"], len(order)))
        self._pending_triggers = []
        primary = triggers[0]
        for trig in triggers:
            self.trace.record("replan_trigger", {**trig, "primary": trig is primary})
        self.state.replan_triggers.extend(triggers)
        key = f"{primary['trigger']}|{primary['world_version']}|{primary['tool']}|{primary['service']}"
        if key in self._replan_keys:
            self.trace.record("replan_suppressed", {"key": key, "reason": "same trigger already re-planned at this world version"})
            return
        self._replan_keys.add(key)
        self._replan(primary, triggers)

    def _replan(self, primary: dict[str, Any], triggers: list[dict[str, Any]]) -> None:
        current = self.state.plan if isinstance(self.state.plan, AgentPlan) else AgentPlan("No active plan", [], 0)
        payload = {**primary, "other_triggers": [t["trigger"] for t in triggers[1:]]}
        summary = self._state_summary()
        use_model = (not self.state.wrap_up and primary["trigger"] != TRIGGER_BUDGET
                     and self.budget.remaining_llm > self.warning_calls + 1)
        plan: Any = None
        source = "deterministic"
        if use_model:
            try:
                plan = self._invoke_model("plan_revision", lambda: self._planner_revise(current, payload, summary, True))
                source = "model"
            except (PermanentModelError, BudgetExceeded):
                raise
            except Exception as exc:  # invalid revision output or provider trouble: keep going deterministically
                if not isinstance(exc, TransientModelError):
                    self._validation_failure("plan_revision", "invalid_plan", _short(exc))
                self.trace.record("plan_revision_fallback", {"error": _short(exc)})
                plan = None
        if not isinstance(plan, AgentPlan):
            plan = self._planner_revise(current, payload, summary, False)
            source = "deterministic"
        if plan is current or plan.revision <= current.revision:
            plan = AgentPlan(plan.hypothesis, list(plan.steps), current.revision + 1, plan.rationale_summary)
        plan = Planner.ensure_terminal_steps(plan)
        self.state.plan = plan
        self._metrics["plan_revisions"] += 1
        self.trace.record("plan_revision", {"revision": plan.revision, "trigger": primary["trigger"], "source": source,
                                            "previous_hypothesis": current.hypothesis, "plan": plan_to_dict(plan)})
        self._note(self._plan_note(plan, source, primary["trigger"]))

    def _planner_revise(self, current: AgentPlan, payload: dict[str, Any], summary: str, allow_model: bool) -> AgentPlan:
        try:
            plan = self.planner.revise(current, payload, summary, allow_model=allow_model,
                                       fallback_on_error=not allow_model)
        except TypeError as exc:  # a planner without the keyword options (duck-typed replacement)
            if "unexpected keyword" not in str(exc):
                raise
            plan = self.planner.revise(current, payload, summary) if allow_model else \
                Planner(self.model).deterministic_revision(current, payload, summary)
        if not isinstance(plan, AgentPlan):
            plan = Planner(self.model).deterministic_revision(current, payload, summary)
        return plan

    # =================================================================== budget + termination
    def _budget_gate(self) -> AgentOutcome | None:
        if self.budget.remaining_tools <= 0:
            self._record_budget_exhaustion("tools", "tool-call budget exhausted")
            return self._terminate("budget_exhausted",
                                   "Tool-call budget exhausted before a safe terminal action; no further actions possible.")
        if self.budget.remaining_llm <= 0:
            return self._safe_finish("LLM-call budget exhausted", exhausted="llm")
        reasons = []
        if self.budget.remaining_llm <= self.warning_calls:
            reasons.append(f"llm_calls_remaining={self.budget.remaining_llm}")
        if self.budget.remaining_tools <= self.warning_calls:
            reasons.append(f"tool_calls_remaining={self.budget.remaining_tools}")
        if self.runtime.remaining <= self.runtime.max_seconds * self._runtime_warning_fraction:
            reasons.append(f"runtime_remaining_s={self.runtime.remaining:.1f}")
        if reasons:
            first = not self.state.wrap_up
            self.trace.record("budget_warning", {"reasons": reasons, "first": first,
                                                 "remaining_llm": self.budget.remaining_llm,
                                                 "remaining_tools": self.budget.remaining_tools,
                                                 "remaining_runtime_s": round(self.runtime.remaining, 2),
                                                 "allowed_tools": sorted(self._allowed_tools())})
            if first:
                self.state.wrap_up = True
                self._queue_trigger(TRIGGER_BUDGET, details={"reasons": reasons})
                self._process_triggers()
            self._note(f"[controller] BUDGET WARNING: {self.budget.remaining_llm} model call(s), "
                       f"{self.budget.remaining_tools} tool call(s), ~{self.runtime.remaining:.0f}s left. Allowed tools now: "
                       f"{sorted(self._allowed_tools())}. Prioritise verify_recovery then close_incident with its evidence, "
                       "or escalate_incident with evidence.")
            self._flush_notes()
        return None

    def _on_budget_exceeded(self, exc: BudgetExceeded) -> AgentOutcome:
        if self.budget.remaining_tools > 0 and self.budget.remaining_llm <= 0:
            return self._safe_finish(str(exc), exhausted="llm")
        self._record_budget_exhaustion("tools" if self.budget.remaining_tools <= 0 else "unknown", str(exc))
        return self._terminate("budget_exhausted", f"{exc}; stopped without a terminal action (incident left open).")

    def _safe_finish(self, reason: str, *, exhausted: str | None) -> AgentOutcome:
        """The model can no longer be consulted usefully (LLM budget spent, or no progress during
        budget wrap-up). Python ends the run safely with tool calls only: close if recovery is
        already proven (verifying first if a remediation is unverified), otherwise hand off to
        humans with evidence. Hard tool/runtime limits still apply (-> budget_exhausted)."""
        if exhausted:
            self._record_budget_exhaustion(exhausted, reason)
        self.trace.record("safe_finish", {"reason": reason, "remaining_tools": self.budget.remaining_tools,
                                          "remaining_llm": self.budget.remaining_llm})
        try:
            proof = self.state.proof_of_recovery()
            if proof is None and self.state.mutations_since_verify > 0 and self.budget.remaining_tools >= 2:
                result = self._run_tool("verify_recovery", {}, origin="controller",
                                        purpose=f"final verification ({reason})")
                self._after_observation("verify_recovery", {}, result)
                proof = self.state.proof_of_recovery()
            if proof is not None and self.budget.remaining_tools >= 1:
                args = {
                    "summary": self._closure_summary(proof),
                    "evidence_ids": self._closure_evidence(proof),
                    "expected_world_version": self.state.latest_world_version,
                    "reason": f"verify_recovery proves recovery; controller closes with that evidence ({reason})"[:300],
                }
                result = self._run_tool("close_incident", args, origin="controller", purpose="deterministic closure with verified evidence")
                if result.get("status") == "ok":
                    return self._terminate("resolved",
                                           f"Resolved: recovery proven by {proof.get('evidence_id')}; the controller closed the "
                                           f"incident because {reason}.")
            if self.budget.remaining_tools >= 1:
                result = self._run_tool("escalate_incident", self._escalation_args(
                    f"Autonomous agent stopped: {reason} before recovery could be proven"),
                    origin="controller", purpose=f"safe handoff ({reason})")
                if result.get("status") == "ok":
                    return self._terminate("escalated",
                                           f"Escalated with evidence by the controller: {reason} before recovery could be proven.")
        except BudgetExceeded as exc:
            self._record_budget_exhaustion("runtime" if isinstance(exc, RuntimeBudgetExceeded) else "tools", str(exc))
        if exhausted is None and self.budget.remaining_tools > 0 and self.budget.remaining_llm > 0:
            return self._terminate("failed", f"{reason}; the safe handoff could not be completed (incident left open).")
        return self._terminate("budget_exhausted", f"{reason}; no safe terminal action could be completed (incident left open).")

    def _record_budget_exhaustion(self, resource: str, detail: str) -> None:
        self.trace.record("budget_exhaustion", {"resource": resource, "detail": detail,
                                                "llm_calls": self.budget.llm_calls, "tool_calls": self.budget.tool_calls,
                                                "elapsed_agent_seconds": round(self.runtime.elapsed, 3)})

    def _terminate(self, status: str, summary: str) -> AgentOutcome:
        if self._outcome is not None:
            return self._outcome
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status {status!r}")
        self.state.status = status
        outcome = AgentOutcome(
            status=status, summary=summary, llm_calls=self.budget.llm_calls, tool_calls=self.budget.tool_calls,
            final_world_version=self.state.latest_world_version, evidence_ids=list(self.state.evidence_ids),
            trace_path=str(getattr(self.trace, "path", "")),
        )
        self._outcome = outcome
        verify = self.state.latest_verify
        self.trace.record("terminal_result", {
            "status": status, "summary": summary, "llm_calls": self.budget.llm_calls, "tool_calls": self.budget.tool_calls,
            "final_world_version": self.state.latest_world_version,
            "elapsed_agent_seconds": round(self.runtime.elapsed, 3),
            "elapsed_wall_seconds": round(self.runtime.wall_seconds, 3),
            "plan_revision": self.state.plan.revision if isinstance(self.state.plan, AgentPlan) else None,
            "replan_triggers": [t["trigger"] for t in self.state.replan_triggers],
            "latest_verify": {"evidence_id": verify.get("evidence_id"), "world_version": verify.get("world_version"),
                              "data": verify.get("data")} if verify else None,
            "remediations": self.state.remediations, "approvals": self.state.approvals,
            "token_usage": self.state.token_usage, "metrics": self._metrics,
        })
        return outcome

    # =================================================================== helpers: messages
    def _append_assistant(self, reply: ModelReply, calls: list[ToolCall]) -> None:
        msg: dict[str, Any] = {"role": "assistant", "content": reply.content if reply.content is not None else ""}
        if calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": str(c.name), "arguments": json.dumps(c.arguments, default=str)}}
                for c in calls
            ]
        self.state.messages.append(msg)

    def _append_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        self.state.messages.append({"role": "tool", "tool_call_id": call.id, "content": _json(result)})

    def _note(self, text: str) -> None:
        self._notes.append(text)

    def _flush_notes(self) -> None:
        if self._notes:
            self.state.messages.append({"role": "user", "content": "\n".join(self._notes)})
            self._notes = []

    def _coerce_reply(self, reply: Any) -> ModelReply:
        if isinstance(reply, ModelReply):
            if not isinstance(reply.tool_calls, list):
                reply.tool_calls = list(reply.tool_calls or [])
            return reply
        return ModelReply(content=None if reply is None else str(reply)[:500])

    def _normalise_calls(self, raw_calls: list[Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, raw in enumerate(raw_calls or []):
            if isinstance(raw, dict):
                function = raw.get("function") or {}
                name, args, call_id = raw.get("name") or function.get("name"), raw.get("arguments", function.get("arguments")), raw.get("id")
            else:
                name, args, call_id = getattr(raw, "name", None), getattr(raw, "arguments", None), getattr(raw, "id", None)
            if isinstance(name, str):
                clean = name.strip()
                if clean.startswith("functions."):
                    clean = clean[len("functions."):]
                if clean != name:
                    self.trace.record("tool_name_normalised", {"raw": name, "normalised": clean})
                name = clean
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    args = parsed if isinstance(parsed, dict) else {"__malformed_arguments__": args}
                except (TypeError, ValueError):
                    args = {"__malformed_arguments__": args}
            elif args is None:
                args = {}
            self._call_seq += 1
            if not isinstance(call_id, str) or not call_id or call_id in self._seen_call_ids:
                call_id = f"call_{self._turn}_{index}_{self._call_seq}"
            self._seen_call_ids.add(call_id)
            calls.append(ToolCall(id=call_id, name=name, arguments=args))
        return calls

    # =================================================================== helpers: results
    def _reject(self, call: ToolCall, code: str, message: str) -> dict[str, Any]:
        is_validation = code in VALIDATION_CODES
        self._metrics["validation_failures" if is_validation else "policy_rejections"] += 1
        self.state.unproductive_streak += 1
        name = call.name if isinstance(call.name, str) else str(call.name)
        self.trace.record("validation_failure" if is_validation else "policy_rejection",
                          {"code": code, "tool": name, "arguments": call.arguments if isinstance(call.arguments, dict) else str(call.arguments),
                           "message": message, "turn": self._turn})
        self.state.blocked_actions.append({"tool": name, "code": code, "turn": self._turn})
        return {"status": "validation_error" if is_validation else "rejected", "tool": name,
                "world_version": self.state.latest_world_version, "evidence_id": None, "data": None,
                "retryable": False, "reason_code": code, "executed": False, "message": message}

    def _policy_block(self, name: str, code: str, message: str) -> dict[str, Any]:
        self._metrics["policy_rejections"] += 1
        self.state.unproductive_streak += 1
        self.trace.record("policy_rejection", {"code": code, "tool": name, "message": message, "turn": self._turn})
        self.state.blocked_actions.append({"tool": name, "code": code, "turn": self._turn})
        return {"status": "rejected", "tool": name, "world_version": self.state.latest_world_version,
                "evidence_id": None, "data": None, "retryable": False, "reason_code": code, "executed": False,
                "message": message}

    def _block_loop(self, call: ToolCall, kind: str) -> dict[str, Any]:
        self._metrics["loop_blocks"] += 1
        self.state.unproductive_streak += 1
        args = call.arguments
        self.trace.record("loop_detected", {"kind": kind, "tool": call.name, "arguments": args, "turn": self._turn,
                                            "limit": self.loop_guard.max_same_action_repeats})
        self.state.blocked_actions.append({"tool": call.name, "code": kind, "turn": self._turn})
        self._queue_trigger(TRIGGER_LOOP, tool=call.name, service=args.get("service"), details={"kind": kind})
        return {"status": "loop_blocked", "tool": call.name, "world_version": self.state.latest_world_version,
                "evidence_id": None, "data": None, "retryable": False, "reason_code": kind, "executed": False,
                "message": f"Loop detected: {call.name} with equivalent arguments already reached the repeat limit "
                           f"({self.loop_guard.max_same_action_repeats}) at this world state. It was NOT executed. "
                           "Use a different observation or action, or escalate."}

    def _stale_result(self, name: str, expected: Any, latest: Any) -> dict[str, Any]:
        return {"status": "stale_precondition", "tool": name, "world_version": latest, "evidence_id": None,
                "data": None, "retryable": True, "expected": expected, "actual": latest, "source": "controller",
                "message": "World changed after your observation (controller precondition check); the action was NOT "
                           "sent. Re-observe before taking this action."}

    def _validation_failure(self, stage: str, code: str, detail: str) -> None:
        self._metrics["validation_failures"] += 1
        self.trace.record("validation_failure", {"stage": stage, "code": code, "detail": detail})

    # =================================================================== helpers: summaries
    def _digest(self, result: dict[str, Any] | None) -> str:
        if not result:
            return "n/a"
        data = result.get("data") or {}
        tool = result.get("tool")
        if result.get("status") != "ok":
            return f"{result.get('status')}: {str(result.get('message'))[:120]}"
        if tool == "get_service_health":
            return f"healthy={data.get('healthy')} replicas={data.get('replicas')} version={data.get('version')}"
        if tool == "get_metrics":
            parts = [f"err={data.get('error_rate')}", f"p95={data.get('p95_ms')}ms", f"cpu={data.get('cpu_pct')}",
                     f"mem={data.get('memory_pct')}", f"replicas={data.get('replicas')}"]
            if "db_connections_pct" in data:
                parts.append(f"db_conn={data['db_connections_pct']}")
            if "cache_hit_rate" in data:
                parts.append(f"cache_hit={data['cache_hit_rate']}")
            return " ".join(parts)
        if tool == "get_logs":
            return "logs: " + " | ".join(str(e) for e in (data.get("entries") or [])[-3:])[:220]
        if tool == "get_deployments":
            current, previous = data.get("current") or {}, data.get("previous") or {}
            return (f"current={current.get('version')} ({current.get('deployed_minutes_ago')}m ago) "
                    f"previous={previous.get('version')}")
        if tool == "get_dependencies":
            return f"depends_on={data.get('depends_on')} called_by={data.get('called_by')}"
        if tool == "get_runbook":
            return f"runbook {data.get('topic')}: {' '.join(data.get('steps') or [])[:160]}"
        if tool == "verify_recovery":
            return (f"criteria_met={data.get('criteria_met')} success={data.get('checkout_success_rate')} "
                    f"p95={data.get('critical_path_p95_ms')}ms healthy={data.get('critical_services_healthy')}")
        return _json(data)[:160]

    def _state_summary(self) -> str:
        incident = self.state.incident or {}
        lines = [
            f"Incident {incident.get('incident_id')} {incident.get('severity')}: {incident.get('title')}; ticket suspects "
            f"{incident.get('suspected_service')} (unverified).",
            f"Current world_version={self.state.latest_world_version}. Budget left: {self.budget.remaining_llm} model calls, "
            f"{self.budget.remaining_tools} tool calls, ~{self.runtime.remaining:.0f}s.",
            "Latest observations:",
        ]
        for service, rows in self.state.latest_by_service.items():
            lines.append(f"- {service}: " + "; ".join(
                f"{r.get('tool')} {r.get('evidence_id')}@v{r.get('world_version')} {self._digest(r)}" for r in rows.values()))
        if self.state.latest_verify:
            v = self.state.latest_verify
            lines.append(f"Latest verify_recovery {v.get('evidence_id')}@v{v.get('world_version')}: {self._digest(v)}")
        for rem in self.state.remediations:
            lines.append(f"Executed {rem['tool']}({_json(rem['arguments'])}) -> {rem.get('effect')} "
                         f"({rem.get('evidence_id')}@v{rem.get('world_version')})")
        for info in self.state.denied_actions.values():
            lines.append(f"DENIED by the human approver: {info['tool']}({_json(info['arguments'])}) x{info['count']}")
        for blocked in self.state.blocked_actions[-3:]:
            lines.append(f"Rejected/blocked proposal: {blocked['tool']} ({blocked['code']})")
        return "\n".join(lines)[:3000]

    def _plan_note(self, plan: AgentPlan, source: str, trigger: str | None = None) -> str:
        steps = " ".join(f"{i}.[{s.status}] {s.objective}" for i, s in enumerate(plan.steps, 1))
        head = f"[controller] Active plan r{plan.revision} ({source}" + (f", trigger={trigger}" if trigger else "") + ")"
        return f"{head}: hypothesis: {plan.hypothesis} | steps: {steps}"[:1400]

    def _update_plan_steps(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        plan = self.state.plan
        if not isinstance(plan, AgentPlan) or result.get("status") != "ok":
            return
        pending = [s for s in plan.steps if s.status == "pending"]

        def text(step: Any) -> str:
            return f"{step.step_id} {step.objective} {step.success_signal}".lower()

        def remediation_step(step: Any) -> bool:
            t = text(step)
            return any(w in t for words in _REMEDIATION_WORDS.values() for w in words) or "remediat" in t or "mitigat" in t

        target = None
        if name == "verify_recovery":
            if (result.get("data") or {}).get("criteria_met"):
                target = next((s for s in pending if _is_verify_step(text(s))), None)
        elif name in TERMINAL_TOOLS:
            target = next((s for s in pending if "close" in text(s) or "escalat" in text(s)), None)
        elif name in REMEDIATION_TOOLS:
            words = _REMEDIATION_WORDS.get(name, ())
            target = next((s for s in pending if name in text(s) or any(w in text(s) for w in words)
                           or "remediat" in text(s) or "mitigat" in text(s)), None)
        else:
            service = args.get("service")
            target = next((s for s in pending
                           if not _is_verify_step(text(s)) and "close" not in text(s) and not remediation_step(s)
                           and (name in text(s) or (isinstance(service, str) and service in text(s)))), None)
        if target is not None:
            target.status = "done"
            self.trace.record("plan_step_update", {"revision": plan.revision, "step_id": target.step_id,
                                                   "status": "done", "evidence_id": result.get("evidence_id")})

    def _closure_evidence(self, proof: dict[str, Any]) -> list[str]:
        ids = [r.get("evidence_id") for r in self.state.remediations if r.get("evidence_id")]
        ids.append(proof.get("evidence_id"))
        return [e for e in dict.fromkeys(ids) if e]

    def _closure_summary(self, proof: dict[str, Any]) -> str:
        actions = ", ".join(f"{r['tool']}({_json(r['arguments'])})" for r in self.state.remediations) or "no remediation"
        return (f"Recovery verified by verify_recovery {proof.get('evidence_id')} ({self._digest(proof)}). "
                f"Remediation: {actions}.")[:600]

    def _escalation_args(self, why: str) -> dict[str, Any]:
        evidence: list[str] = [r.get("evidence_id") for r in self.state.observations]
        evidence.extend(r.get("evidence_id") for r in self.state.remediations)
        if self.state.latest_verify:
            evidence.append(self.state.latest_verify.get("evidence_id"))
        evidence = [e for e in dict.fromkeys(evidence) if e][-12:]
        unhealthy = sorted(s for s, rows in self.state.latest_by_service.items()
                           if any(observed_health(r, self.objective) is False for r in rows.values()))
        denied = [info["tool"] for info in self.state.denied_actions.values()]
        reason = (f"{why}. Unhealthy components observed: {unhealthy or 'none confirmed'}. "
                  f"Remediations executed: {[r['tool'] for r in self.state.remediations] or 'none'}. "
                  f"Denied actions: {denied or 'none'}. Human follow-up required.")
        return {"reason": reason[:900], "evidence_ids": evidence}
