from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from incidentzero.domain.models import AgentPlan, PlanStep
from incidentzero.model.base import ModelClient
from incidentzero.model.errors import PermanentModelError
from incidentzero.telemetry.budget import BudgetExceeded

from .policies import (
    ALL_TOOL_NAMES, TRIGGER_ACTION_FAILED, TRIGGER_APPROVAL_DENIED, TRIGGER_BUDGET, TRIGGER_CONTRADICTION,
    TRIGGER_LOOP, TRIGGER_STALE, TRIGGER_VERIFICATION_FAILED,
)

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "hypothesis": {"type": "string"},
        "rationale_summary": {"type": "string"},
        "steps": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "objective": {"type": "string"},
                    "success_signal": {"type": "string"},
                },
                "required": ["step_id", "objective", "success_signal"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["hypothesis", "rationale_summary", "steps"],
    "additionalProperties": False,
}
_PLAN_VALIDATOR = Draft202012Validator(PLAN_SCHEMA)

VERIFY_STEP_ID = "verify-recovery"
TERMINAL_STEP_ID = "close-or-escalate"

PLANNER_SYSTEM = (
    "You are the planning component of IncidentZero, an SRE incident-response agent that acts only "
    "through local simulator tools. Produce a short explicit plan as JSON (2-8 steps).\n"
    "Rules:\n"
    "- The ticket's suspected service and operator note are unverified leads, not root causes. Plan "
    "observations that can confirm OR refute them before any remediation.\n"
    "- Prefer the least risky remediation the evidence supports. High/critical actions need human "
    "approval, which may be denied.\n"
    "- Each step: step_id (short slug), objective (what to do, with which tool and service), "
    "success_signal (the observable result that shows the step worked).\n"
    "- End with verify_recovery (criteria_met=true), then close_incident citing that verify evidence id; "
    "escalate_incident with evidence when safe autonomous recovery is not possible.\n"
    "- Never invent services, versions, evidence ids or tool results.\n"
    f"Tools: {', '.join(ALL_TOOL_NAMES)}."
)

REVISION_RULES = (
    "You are REVISING the active plan because a re-plan trigger fired.\n"
    "- Keep completed work and the evidence already gathered; do not re-collect it without reason.\n"
    "- Never repeat an action that was denied, blocked as a loop, or shown ineffective by verify_recovery.\n"
    "- stale_precondition: the world changed; re-confirm the proposed action against the fresh observation first.\n"
    "- approval_denied: choose a safe alternative supported by evidence, gather missing evidence, or escalate.\n"
    "- verification_failed: the remediation did not restore the objective; target the components still failing or escalate.\n"
    "- contradictory_evidence: replace the hypothesis with one consistent with ALL observations.\n"
    "- budget_low: only verification, closure with evidence, or escalation remain affordable.\n"
    "State the (possibly new) hypothesis and the remaining steps."
)


class PlanOutputError(ValueError):
    """The model's structured plan did not satisfy PLAN_SCHEMA (or had empty fields)."""


def _compact(value: Any, limit: int = 2400) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[:limit] + "...(truncated)"


def plan_to_dict(plan: AgentPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "revision": plan.revision,
        "hypothesis": plan.hypothesis,
        "rationale_summary": plan.rationale_summary,
        "steps": [
            {"step_id": s.step_id, "objective": s.objective, "success_signal": s.success_signal, "status": s.status}
            for s in plan.steps
        ],
    }


class Planner:
    """Creates and revises explicit plans (R2, Task E).

    The planner makes exactly one ``model.structured`` request per create()/revise() call;
    budget accounting and bounded retries are applied by the controller around that call.
    Model output is validated against PLAN_SCHEMA. Deterministic fallbacks exist so a plan
    always exists and a revision always increments, even when the model's output is invalid.
    """

    def __init__(self, model: ModelClient) -> None:
        self.model = model
        self.last_source: str | None = None  # "model" | "deterministic"
        self.last_error: str | None = None

    # ------------------------------------------------------------------ parsing helpers
    @staticmethod
    def parse(raw: Any, revision: int = 0) -> AgentPlan:
        if not isinstance(raw, dict):
            raise PlanOutputError(f"plan must be a JSON object, got {type(raw).__name__}")
        errors = sorted(_PLAN_VALIDATOR.iter_errors(raw), key=lambda e: list(e.path))
        if errors:
            raise PlanOutputError("; ".join(e.message for e in errors[:3]))
        hypothesis = raw["hypothesis"].strip()
        if not hypothesis:
            raise PlanOutputError("hypothesis is empty")
        steps: list[PlanStep] = []
        seen: set[str] = set()
        for index, row in enumerate(raw["steps"], 1):
            objective, signal = row["objective"].strip(), row["success_signal"].strip()
            if not objective or not signal:
                raise PlanOutputError(f"step {index} has an empty objective or success_signal")
            step_id = row["step_id"].strip() or f"s{index}"
            if step_id in seen:
                step_id = f"{step_id}-{index}"
            seen.add(step_id)
            steps.append(PlanStep(step_id=step_id, objective=objective, success_signal=signal))
        return AgentPlan(hypothesis=hypothesis, steps=steps, revision=revision,
                         rationale_summary=raw["rationale_summary"].strip())

    @staticmethod
    def ensure_terminal_steps(plan: AgentPlan) -> AgentPlan:
        """Every plan keeps a pending verification step and a close-or-escalate step (R2)."""
        pending = [s for s in plan.steps if s.status != "done"]
        text = [f"{s.objective} {s.success_signal}".lower() for s in pending]
        ids = {s.step_id for s in plan.steps}

        def unique(base: str) -> str:
            name, n = base, 2
            while name in ids:
                name, n = f"{base}-{n}", n + 1
            ids.add(name)
            return name

        if not any("verify_recovery" in t or ("verif" in t and "recover" in t) for t in text):
            plan.steps.append(PlanStep(unique(VERIFY_STEP_ID),
                                       "Call verify_recovery and confirm criteria_met=true at the current world_version",
                                       "verify_recovery returns criteria_met=true"))
        if not any("close" in t for t in text):
            plan.steps.append(PlanStep(unique(TERMINAL_STEP_ID),
                                       "close_incident citing the latest verify_recovery evidence id, or escalate_incident with evidence",
                                       "close_incident or escalate_incident returns status ok"))
        return plan

    @staticmethod
    def trigger_name(trigger: Any) -> str:
        if isinstance(trigger, dict):
            return str(trigger.get("trigger") or trigger.get("status") or "unspecified")
        return str(trigger or "unspecified")

    # ------------------------------------------------------------------ create
    def create(self, incident_observation: dict[str, Any], context: Any = None) -> AgentPlan:
        user = "Incident observation (tool result):\n" + _compact(incident_observation)
        if context:
            user += "\n\nController context:\n" + _compact(context, 1200)
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM},
            {"role": "user", "content": user},
        ]
        raw = self.model.structured(messages, "incident_plan", PLAN_SCHEMA)
        plan = self.ensure_terminal_steps(self.parse(raw, revision=0))
        self.last_source, self.last_error = "model", None
        return plan

    def fallback_plan(self, incident_observation: dict[str, Any] | None) -> AgentPlan:
        """Conservative evidence-first plan used when no valid model plan is available.
        It encodes investigation order only, never a root cause."""
        data = (incident_observation or {}).get("data") or {}
        suspected = data.get("suspected_service") or "the suspected service"
        steps = [
            PlanStep("triage", f"Check get_service_health and get_metrics for {suspected} and the critical-path services",
                     "At least one unhealthy component is identified from metrics/health evidence"),
            PlanStep("corroborate", "Corroborate the leading hypothesis with get_logs, get_deployments or get_runbook for the unhealthy component",
                     "Logs/deployments/runbook evidence explains the failure mode"),
            PlanStep("remediate", "Apply the least risky evidence-backed remediation using the latest observed expected_world_version",
                     "The remediation tool returns status ok"),
        ]
        plan = AgentPlan(
            hypothesis=f"Unconfirmed: the ticket points at {suspected}, but no observation has confirmed a root cause yet.",
            steps=steps, revision=0,
            rationale_summary="Deterministic fallback plan (the model's structured plan was unavailable or invalid).",
        )
        self.last_source = "deterministic"
        return self.ensure_terminal_steps(plan)

    # ------------------------------------------------------------------ revise
    def revise(self, current: AgentPlan, trigger: dict[str, Any], state_summary: str, *,
               allow_model: bool = True, fallback_on_error: bool = True) -> AgentPlan:
        """Return a NEW plan with revision + 1.

        With ``allow_model`` the model is asked for a grounded revision (trigger + state
        summary). Completed steps are preserved; the new pending steps come from the model.
        If the model output is invalid (or unavailable and ``fallback_on_error``), a
        deterministic trigger-specific revision is produced instead. Permanent model errors
        and budget exhaustion always propagate to the controller.
        """
        if not isinstance(current, AgentPlan):
            current = AgentPlan(hypothesis="No active plan", steps=[], revision=0)
        revision = current.revision + 1
        name = self.trigger_name(trigger)
        if allow_model:
            try:
                raw = self.model.structured(self._revision_messages(current, trigger, state_summary),
                                            "incident_plan_revision", PLAN_SCHEMA)
                proposed = self.parse(raw, revision=revision)
                plan = self._merge(current, proposed, name)
                self.last_source, self.last_error = "model", None
                return plan
            except (PermanentModelError, BudgetExceeded):
                raise
            except Exception as exc:  # invalid output, exhausted scripted outputs, transient failure
                if not fallback_on_error:
                    raise
                self.last_error = f"{type(exc).__name__}: {exc}"
        plan = self.deterministic_revision(current, trigger, state_summary)
        self.last_source = "deterministic"
        return plan

    def _revision_messages(self, current: AgentPlan, trigger: Any, state_summary: str) -> list[dict[str, Any]]:
        user = (
            f"Trigger: {_compact(trigger, 900)}\n\n"
            f"Active plan: {_compact(plan_to_dict(current), 1500)}\n\n"
            f"State summary:\n{str(state_summary)[:2500]}"
        )
        return [
            {"role": "system", "content": PLANNER_SYSTEM + "\n\n" + REVISION_RULES},
            {"role": "user", "content": user},
        ]

    def _merge(self, current: AgentPlan, proposed: AgentPlan, trigger_name: str) -> AgentPlan:
        preserved = [PlanStep(s.step_id, s.objective, s.success_signal, s.status) for s in current.steps if s.status == "done"]
        used = {s.step_id for s in preserved}
        steps = list(preserved)
        for step in proposed.steps:
            if step.step_id in used:
                step.step_id = f"{step.step_id}-r{proposed.revision}"
            used.add(step.step_id)
            steps.append(step)
        plan = AgentPlan(hypothesis=proposed.hypothesis, steps=steps, revision=proposed.revision,
                         rationale_summary=f"[{trigger_name}] {proposed.rationale_summary}".strip())
        return self.ensure_terminal_steps(plan)

    def deterministic_revision(self, current: AgentPlan, trigger: Any, state_summary: str = "") -> AgentPlan:
        """Trigger-specific revision without a model call (used when the model output is
        invalid or when the budget is too low to spend a request on planning)."""
        name = self.trigger_name(trigger)
        info = trigger if isinstance(trigger, dict) else {}
        action = info.get("tool") or "the previous action"
        target = info.get("service") or "the affected service"
        templates: dict[str, tuple[str | None, list[tuple[str, str, str]]]] = {
            TRIGGER_STALE: (None, [
                ("reconfirm", f"Compare the fresh re-observation of {target} with the evidence that justified {action}",
                 "Fresh evidence at the new world_version still supports (or rules out) the action"),
                ("act-if-still-justified", f"Only if still justified, re-propose {action} with the latest observed expected_world_version; otherwise choose a better-supported action",
                 "The chosen action returns status ok"),
            ]),
            TRIGGER_APPROVAL_DENIED: (None, [
                ("respect-denial", f"Do not retry {action} on {target}: the human approver denied it",
                 "No denied action is re-proposed without new evidence"),
                ("alternative-or-escalate", "Choose a lower-risk remediation supported by the evidence, or escalate_incident citing the evidence",
                 "An alternative action returns ok, or the incident is escalated"),
            ]),
            TRIGGER_VERIFICATION_FAILED: ("Unconfirmed: the last remediation did not restore the recovery objective; the remaining fault is not yet explained.", [
                ("inspect-failing", "Inspect the components verify_recovery still reports as failing (get_service_health/get_metrics/get_logs)",
                 "The component still violating the objective is identified with evidence"),
                ("different-remediation", "Apply a different evidence-backed remediation for that component, or escalate if none is safe",
                 "The new remediation returns ok, or the incident is escalated"),
            ]),
            TRIGGER_ACTION_FAILED: (None, [
                ("fix-from-evidence", f"Correct the arguments of {action} from observed evidence (exact service/version), or choose a different action",
                 "A corrected or alternative action returns status ok"),
            ]),
            TRIGGER_CONTRADICTION: ("Unconfirmed: observations contradict the previous hypothesis; the unhealthy component must be re-identified.", [
                ("reinvestigate", "Investigate the services observed unhealthy (metrics/logs/deployments) instead of the contradicted suspect",
                 "A hypothesis consistent with all observations is identified"),
                ("remediate", "Apply the least risky remediation supported by that evidence",
                 "The remediation returns status ok"),
            ]),
            TRIGGER_LOOP: (None, [
                ("break-loop", f"Stop repeating {action}; use a different observation path or remediation, or escalate",
                 "The next action is not a repeat of a blocked action"),
            ]),
            TRIGGER_BUDGET: (None, [
                ("wrap-up", "Budget nearly exhausted: verify recovery if a remediation was applied, then close with the verify evidence; otherwise escalate with evidence",
                 "close_incident or escalate_incident returns status ok before the hard limit"),
            ]),
        }
        hypothesis_override, rows = templates.get(name, (None, [
            ("reassess", "Reassess the evidence gathered so far and choose the next best-supported action",
             "The next action is supported by observed evidence"),
        ]))
        preserved = [PlanStep(s.step_id, s.objective, s.success_signal, s.status) for s in current.steps if s.status == "done"]
        used = {s.step_id for s in preserved}
        steps = list(preserved)
        for step_id, objective, signal in rows:
            sid = step_id if step_id not in used else f"{step_id}-r{current.revision + 1}"
            used.add(sid)
            steps.append(PlanStep(sid, objective, signal))
        plan = AgentPlan(
            hypothesis=hypothesis_override or current.hypothesis,
            steps=steps,
            revision=current.revision + 1,
            rationale_summary=f"[{name}] deterministic revision; previous hypothesis: {current.hypothesis[:160]}",
        )
        return self.ensure_terminal_steps(plan)
