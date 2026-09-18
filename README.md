# IncidentZero Starter Repository

**Assignment 1 - Agentic Artificial Intelligence (Fall 2026)**  
**Domain:** bounded autonomous SRE / production-incident response  
**Mode:** individual assignment, plain Python + Groq SDK, no agent framework

This repository deliberately gives you a **working simulated production environment** and an **incomplete agent runtime**. Your job is not to build an API or a dashboard. Your job is to turn the baseline loop into a reliable agent that can observe, plan, act, verify, re-plan, recover from failures, respect human approval, and stop correctly under a strict budget.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # Windows CMD
# cp .env.example .env   # Linux/macOS
```

Put your own key in `.env` or set `GROQ_API_KEY` in the shell. **Never submit the key.**

Generate your deterministic public scenario:

```bash
python scripts/generate_student_scenario.py --student-id 22I-1234 --scenario public-a
```

Validate the protected infrastructure (does not call Groq):

```bash
pytest -q tests/public -m infrastructure
python scripts/check_banned_imports.py
python scripts/check_protected_integrity.py
```

Run the full public test suite while developing:

```bash
pytest -q tests/public
```

Several student-requirement tests are expected to fail in the untouched starter. They are specifications, not bugs in the simulator.

Live run with Groq:

```bash
python -m incidentzero.cli run --student-id 22I-1234 --scenario public-a --model openai/gpt-oss-20b
```

## Stable contract

Read `docs/CONTRACTS.md` before editing. Hidden grading assumes those public interfaces still exist. You may refactor internally, but do not delete or rename required public classes/functions.

## Protected areas

Do not modify the simulator to make scenarios easier. The grader uses clean copies and additional hidden scenarios. In particular, do not depend on private fields or anything named `_oracle`, `_root_cause`, or `_scenario_spec`.

The point is to build a robust **controller**, not to reverse-engineer the answer from the simulator source.

---

## Submission: reliable controller (roll no. i25-8029)

The simulator and protected files are unchanged (`python scripts/check_protected_integrity.py`). The agent runtime lives in `incidentzero/agent/`:

| Module | Responsibility |
|---|---|
| `controller.py` | Bounded run loop, 9-stage validation boundary, policy-driven human approval, stale-world handling, re-plan triggers, budget wrap-up, safe finish, full JSONL tracing |
| `planner.py` | Schema-validated plan creation/revision (`PLAN_SCHEMA`), deterministic fallback plan and trigger-specific revisions |
| `policies.py` | `RiskPolicy` (from `configs/risk_policy.json`), `ReplanPolicy` (named triggers), `LoopGuard` (exact + semantic repeats) |
| `recovery.py` | `RetryPolicy`: bounded exponential backoff for transient model errors only, honours Retry-After |
| `state.py` | Working memory: evidence index per service, latest verification, remediations, approvals, denials |
| `config.py` | Loads `configs/limits.json` and `configs/services.json` (no hard-coded budgets) |

Supporting changes: `telemetry/budget.py` (runtime budget with injectable clock; human approval wait excluded but traced), `model/groq_client.py` (SDK retries disabled so every request is budgeted; network/429/5xx/invalid-output classified as transient; server-side `tool_use_failed` returned as corrective feedback), `cli.py` (limits from config, `--approval console|deny`, `--trace-dir`).

### Setup

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1      Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
# Windows: copy .env.example .env      Linux/macOS: cp .env.example .env
# then edit .env and set GROQ_API_KEY=<your key>   (never commit .env)
```

### Offline verification (no Groq calls)

```bash
pytest -q tests/public                 # starter infrastructure + student-requirement tests
pytest -q tests/student                # 24 student tests (Task G), all scripted/offline
pytest -q tests                        # everything
python scripts/check_banned_imports.py
python scripts/check_protected_integrity.py
python scripts/count_todos.py
python scripts/offline_failure_demos.py   # scripted rehearsal traces -> traces/offline/ (clearly labelled, fixture IDs only)
```

### Live evaluation runs (Groq)

```bash
python -m incidentzero.cli run --student-id i25-8029 --scenario public-a --model openai/gpt-oss-20b
python -m incidentzero.cli run --student-id i25-8029 --scenario public-b --model openai/gpt-oss-20b
python -m incidentzero.cli run --student-id i25-8029 --scenario public-c --model openai/gpt-oss-20b
```

High/critical actions stop at a console prompt showing the action, risk level and evidence; type `APPROVE` to allow it, anything else denies it. Traces are written to `traces/<student>_<scenario>.jsonl` (a re-run overwrites the same file). To exercise the denial path live: add `--approval deny --trace-dir traces/denial`.

Fill the REPORT.md evaluation table from the traces (numbers are read from the trace, never estimated):

```bash
python scripts/summarize_trace.py --update-report REPORT.md traces/i25-8029_public-a.jsonl traces/i25-8029_public-b.jsonl traces/i25-8029_public-c.jsonl
```

### Operational notes

- Budgets come only from `configs/limits.json`: 14 model requests (planning, revisions and retries included), 28 tool calls (controller-initiated evidence gathering included), 120 s of agent runtime. A warning and wrap-up mode start when 3 requests remain.
- Groq free-tier limits are per model and per organisation (requests and tokens per minute/day; see console.groq.com/settings/limits). A 429 is retried with backoff up to 3 attempts and then the run aborts cleanly; wait a minute before re-running.
- Outcomes: `resolved` only after a successful `close_incident` backed by a current `verify_recovery` with `criteria_met=true`; `escalated` after a successful `escalate_incident`; `aborted` on permanent model errors or exhausted model retries; `budget_exhausted` when a hard tool/runtime limit stops the run; `failed` when the model makes no executable progress.
