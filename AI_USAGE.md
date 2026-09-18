# AI Assistance Declaration

> DRAFT prepared with AI help. Before submitting, check every statement below against what you actually did, rewrite it in your own words, and delete this note.

Name: [YOUR FULL NAME]
Roll number: i25-8029

## Tools used
Claude (Anthropic), through the claude.ai chat interface.

## What I used them for
- Turning the brief (R1-R14, Tasks A-G) and the starter repository into a controller design.
- Generating most of the code in `incidentzero/agent/` (controller, planner, policies, recovery, state, config), the changes to `telemetry/budget.py`, `model/groq_client.py` and `cli.py`, and the helper scripts `scripts/summarize_trace.py` and `scripts/offline_failure_demos.py`.
- Designing the 24 offline student tests in `tests/student/` and running them against scripted models.
- Drafting the README section, the engineering report and this declaration, which I reviewed and edited.

## Two suggestions I rejected or changed
1. Reporting offline/scripted runs as evaluation results. I required that the public-a/b/c numbers come only from my own live Groq runs. The report table is therefore generated from the live traces by `scripts/summarize_trace.py`, and the scripted rehearsal traces use fixture IDs (TEST-001/TEST-003), never my own scenarios, and are labelled as scripted everywhere.
2. Ending a run as `failed` when the model makes no progress during budget wrap-up. The first version did this even with most of the tool budget left, which abandoned the incident without a handoff. It was changed so the controller performs the safe finish instead: verify an unverified fix, close only with proof, otherwise escalate with evidence.

## One AI-generated or AI-assisted bug I personally diagnosed
Symptom: `test_llm_budget_pressure_escalates_safely_before_the_hard_limit` failed because the controller's own `escalate_incident` call cited an empty `evidence_ids` list.
Cause: the escalation helper collected evidence only from per-service observations, the latest verification and remediations. In that run every model proposal was rejected during budget wrap-up, so the only observation was the incident ticket, which has no service and was skipped.
Fix: build the evidence list from every successful observation (including `get_incident`) plus remediations and the latest verification, most recent twelve IDs.
(Reproduce it yourself before claiming it: in `_escalation_args` in `controller.py`, build the list from `latest_by_service` only and run `pytest -q tests/student -k budget_pressure`.)

## Code ownership statement
I can explain every submitted component, its failure behavior, and the trade-offs I chose. I understand that the TA may ask me to modify the code during viva.

Signature / typed name: [YOUR FULL NAME]
