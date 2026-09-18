# Engineering Report - Assignment 1: IncidentZero

**Student:** Nikhat Raghna - roll no. i25-8029 - **Model:** `openai/gpt-oss-20b` on Groq

**Provenance of numbers.** The public-a/b/c table in Section 5 is generated from the live traces by `scripts/summarize_trace.py`; nothing in it is estimated. The three failure traces in Section 6 are *offline scripted rehearsals*: the real controller, registry and simulator driven by a `ScriptedModelClient` with fixture IDs (TEST-001, TEST-003), stored in `traces/offline/`.

## 1. Architecture

`AgentController.run()` is one bounded loop. The controller reads the ticket itself (`get_incident`), obtains a structured plan, then repeatedly asks the model for exactly one tool call, validates and authorizes it in Python, executes it through `ToolRegistry`, records the result and decides whether to retry, re-plan, stop or continue. The model only proposes; execution, approval, retries, re-planning and termination are Python decisions driven by `configs/limits.json` and `configs/risk_policy.json`.

`AgentState` holds the conversation, the plan with step statuses, every observed evidence ID, the newest world version, the latest successful observation per service and tool, the latest `verify_recovery` result, executed remediations, approvals, denials and rejected proposals. This index drives precondition checks, approval justifications and re-planning summaries.

Every proposal crosses a nine-stage boundary before touching the simulator: known tool name; arguments are a JSON object; JSON-schema validation; permitted values (services from `services.json`); budget policy; loop detection; state preconditions (world version, pending re-observation, proof before closure); evidence requirements for remediations; human approval inside the single execution path. A rejection returns a structured corrective observation (`validation_error`, `rejected` or `loop_blocked` with a reason code) and is traced.

`RetryPolicy` retries only transient model errors; the Groq SDK's hidden retries are disabled so every request is budgeted and traced. The trace records each model request and attempt, reply, proposal, validation failure, approval request and result, tool result (origin model or controller), retry, stale event, re-plan trigger, revision, budget warning or exhaustion, and the terminal result. The agent sees only public tool results; tool categories come from the tool schemas, never simulator internals.

## 2. Planning and re-planning strategy

The first plan comes from a strict JSON-schema request and is validated again locally; every plan keeps a pending `verify_recovery` step and a close-or-escalate step. If the output is invalid, a deterministic fallback plan encodes only investigation order, never a root cause.

`ReplanPolicy` names seven triggers: `stale_precondition`; `approval_denied`; `non_retryable_action_failure` (mutating tools only); `verification_failed` (only if a remediation ran since the previous verification, so a baseline check does not count); `contradictory_evidence` (every service named in the hypothesis observed healthy while another observed service violates the objective, before any remediation, once per revision); `loop_detected`; `budget_low`. Transient errors are retried and validation errors get corrective feedback; neither re-plans. Several triggers in one turn yield one revision chosen by priority, and an identical trigger at the same world version is not re-planned twice.

A revision always increments the revision number and preserves completed steps, the evidence index, remediations, denials and loop counters; only the hypothesis and pending steps change. The model writes a revision from a grounded state summary only when at least five requests remain; otherwise a free, trigger-specific deterministic revision is used.

## 3. Failure handling

**Malformed model output.** Non-JSON arguments are rejected before execution. Groq's server-side `tool_use_failed` response becomes a turn without an executable call plus corrective feedback, not an abort. Three consecutive turns without progress end the run as `failed`.

**Transient model failure.** Network errors, 408/409/429 and 5xx are transient: at most three attempts per request, backoff 0.5 s doubling to an 8 s cap, provider Retry-After honoured, never sleeping past the runtime budget. Every attempt consumes budget. Exhausted retries and permanent errors (e.g. 401) abort with no further tool calls.

**Transient tool failure.** Read-only telemetry calls get up to three attempts while one tool call stays in reserve; afterwards the model is told to use another observation path. Mutating actions are never retried automatically.

**Stale world version.** A known-stale version is never forwarded or silently replaced; the simulator's own stale rejection is handled identically. The controller re-observes the target (or re-verifies before a close), blocks versioned actions until a fresh observation succeeds, resets semantic repeat counts after an external change, and re-plans; the model must re-confirm the action.

**Approval denial.** The action is not executed and triggers a revision; it is not re-submitted unless new evidence was gathered, and never more than twice.

**Impossible tasks.** Failed verifications after remediation steer the plan to escalation with evidence; without proof the controller never closes.

**Budget exhaustion.** With three requests or tool calls left, or about 26 s of runtime, the controller warns, revises deterministically and restricts tools to verification, affordable remediations and terminal actions. When requests run out, Python finishes alone: verify any unverified remediation, close if recovery is proven, otherwise escalate with evidence. Hard tool or runtime exhaustion ends as `budget_exhausted`.

## 4. Safety and stopping

Approval requirements come only from the risk policy: `rollback_deployment` and `shift_traffic` (high) and `failover_database` (critical) need a human, and a changed level applies on the next run without code edits. A high or critical remediation also needs a metrics, logs or health observation of its target; missing evidence is gathered with read-only tools before the approver is asked, and a rollback must target the previous version from `get_deployments`. Gateway exceptions count as denial.

Proof of recovery is the latest `verify_recovery` with `criteria_met=true` at the current world version with no remediation after it. Otherwise `close_incident` is rejected before execution; when allowed, never-observed evidence IDs are dropped and the proving verification ID is added. `resolved` requires an accepted `close_incident`; `escalated` an accepted `escalate_incident`. Text is never evidence.

## 5. Evaluation

<!-- EVAL-TABLE:START -->
_Source: live Groq runs (traces/*.jsonl), generated by scripts/summarize_trace.py_

| Scenario | Terminal outcome | LLM requests (by operation) | Tool calls | Actions executed | Plan revisions (triggers) | High-risk proposed / approved-denied / executed | Recovery evidence; close result | Avoidable calls | Wall-clock | Tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| i25-8029_public-a | aborted | 8 (plan_create=1, decide=7) | 5 (controller=1) | 0 (none) | 0 | 0 / 0a-0d / 0 | None criteria_met=None; close=not called | 0 repeated reads, 0 rejected | 19.866s wall / 19.866s agent (approval wait 0.0s) | 8625 |
| i25-8029_public-b | escalated | 14 (plan_create=1, decide=13) | 6 (controller=3) | 2 (scale_service, escalate_incident) | 1 (budget_low) | 0 / 0a-0d / 0 | EV-0005 criteria_met=False; close=not called | 0 repeated reads, 0 rejected | 55.826s wall / 55.826s agent (approval wait 0.0s) | 6475 |
| i25-8029_public-c | aborted | 11 (plan_create=1, decide=10) | 4 (controller=1) | 1 (clear_cache) | 0 | 0 / 0a-0d / 0 | None criteria_met=None; close=not called | 0 repeated reads, 0 rejected | 47.555s wall / 47.555s agent (approval wait 0.0s) | 6540 |
<!-- EVAL-TABLE:END -->

Offline, all 8 public and 24 student tests pass with scripted models. Scripted rehearsal results (not live):

| Rehearsal | Outcome | LLM requests | Tool calls | Revisions |
|---|---|---|---|---|
| Denial (TEST-001/public-n) | escalated | 6 | 4 | 1 (approval_denied) |
| Stale (TEST-001/public-f) | resolved | 8 | 8 (2 controller) | 1 (stale_precondition) |
| Verify failed (TEST-001/public-a) | resolved | 10 | 9 | 1 (verification_failed) |

## 6. Three failure traces

**A - Approval denial.** The model read `get_deployments` (EV-0002: current 2.4.1, previous 2.4.0) and checkout-service metrics (EV-0003: error rate 0.19, p95 2,850 ms), then proposed a rollback. The approver saw both evidence IDs in the justification and denied it. The rollback never reached the simulator; the denial produced revision 1, which kept the diagnosis but replaced the fix with escalation. Lesson: a denial is an observation, not an obstacle to retry. The agent escalated citing EV-0002 and EV-0003.

**B - Stale world.** After two observations of redis-cache, the model proposed `clear_cache` at world version 1, but a scheduled event fired on that call and the simulator rejected it as stale (EV-0004, actual version 2). The controller re-observed redis-cache (EV-0005, still unhealthy at v2) instead of patching the number, then triggered revision 1. The model re-confirmed and re-issued the fix at v2 (EV-0006); verification passed (EV-0007: success 0.995, p95 260 ms) and the close was accepted with EV-0007 appended by the controller. Lesson: an external change invalidates preconditions, so evidence is refreshed before acting.

**C - Failed verification.** The first hypothesis blamed cart-service. Its restart executed (EV-0003), but `verify_recovery` (EV-0004) still showed success 0.86 and p95 1,000 ms. Because a remediation had run since the last verification, this was a `verification_failed` trigger: revision 1 moved the hypothesis to the shared cache, the model inspected redis-cache (EV-0005, EV-0006), cleared it (EV-0007) and verification passed (EV-0008). Lesson: an executed action is not a fix; only verification decides, and the wasted restart cost budget.

## 7. Limitations

1. Contradiction detection and plan-step tracking are keyword heuristics: they depend on service names appearing in the hypothesis and on step wording, so a vague plan may never trigger a contradiction revision or may mark a step done early.
2. Fourteen requests is tight. Plan creation and each model-written revision cost a request, and late revisions fall back to generic deterministic text.
3. Controller-initiated evidence gathering and re-observation are real tool calls. They consume budget and, because any tool call can fire a scheduled event, can themselves cause another stale rejection.
4. Validation proves an action is well-formed, authorized and consistent with observations, not that it is the right fix; a wrong but harmless action still executes and costs budget.
5. Approval wait is excluded from runtime, so a slow approver can leave the justification's evidence outdated; the world-version check still blocks execution on a changed world.
6. Tests use scripted models; live behaviour depends on gpt-oss-20b and Groq rate limits, which can end a run as `aborted`.
