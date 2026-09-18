"""Summarise IncidentZero trace files into the REPORT.md evaluation table.

Usage:
    python scripts/summarize_trace.py traces/i25-8029_public-a.jsonl traces/i25-8029_public-b.jsonl traces/i25-8029_public-c.jsonl
    python scripts/summarize_trace.py --update-report REPORT.md traces/i25-8029_public-*.jsonl

Every number is read from the trace itself (terminal_result, tool_result, approval_*, plan_revision
events); nothing is estimated. "Repeated observations" counts read-only calls that re-read the same
tool+arguments at an unchanged world version (the main source of avoidable calls), and "rejected"
counts proposals the controller refused before execution.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

MUTATING = {"restart_service", "scale_service", "clear_cache", "rollback_deployment", "failover_database",
            "shift_traffic", "close_incident", "escalate_incident"}
READ_ONLY = {"get_incident", "get_service_health", "get_metrics", "get_logs", "get_deployments",
             "get_dependencies", "get_runbook", "verify_recovery"}
START, END = "<!-- EVAL-TABLE:START -->", "<!-- EVAL-TABLE:END -->"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarise(path: Path) -> dict:
    rows = load(path)
    events = [(r["event"], r["payload"]) for r in rows]
    terminal = next((p for e, p in reversed(events) if e == "terminal_result"), {})
    metrics = terminal.get("metrics", {})
    revisions = [p for e, p in events if e == "plan_revision"]
    approvals = [p for e, p in events if e == "approval_result"]
    tool_results = [p for e, p in events if e == "tool_result"]
    seen: Counter = Counter()
    repeats = 0
    for p in tool_results:
        name, args = p["call"]["name"], p["call"]["arguments"]
        if name in READ_ONLY and p["result"].get("status") == "ok":
            key = (name, json.dumps(args, sort_keys=True), p["result"].get("world_version"))
            seen[key] += 1
            repeats += seen[key] > 1
    rejected = sum(1 for e, p in events
                   if e in ("policy_rejection", "loop_detected") or (e == "validation_failure" and "stage" not in p))
    verify = terminal.get("latest_verify") or {}
    close = [p for p in tool_results if p["call"]["name"] == "close_incident"]
    high_executed = metrics.get("high_risk_executed", 0)
    actions = [p["call"]["name"] for p in tool_results if p["call"]["name"] in MUTATING and p["result"].get("status") == "ok"]
    return {
        "trace": path.name,
        "outcome": terminal.get("status", "n/a"),
        "llm": f"{terminal.get('llm_calls', '?')} ({', '.join(f'{k}={v}' for k, v in metrics.get('model_requests', {}).items())})",
        "tools": f"{terminal.get('tool_calls', '?')} (controller={metrics.get('controller_tool_calls', 0)})",
        "actions": f"{len(actions)} ({', '.join(actions) or 'none'})",
        "revisions": f"{len(revisions)}" + (f" ({', '.join(p['trigger'] for p in revisions)})" if revisions else ""),
        "high_risk": (f"{metrics.get('high_risk_proposed', 0)} / "
                      f"{sum(1 for a in approvals if a['approved'])}a-{sum(1 for a in approvals if not a['approved'])}d / "
                      f"{high_executed}"),
        "recovery": (f"{verify.get('evidence_id')} criteria_met={(verify.get('data') or {}).get('criteria_met')}; "
                     f"close={close[-1]['result'].get('status') if close else 'not called'}"),
        "avoidable": f"{repeats} repeated reads, {rejected} rejected",
        "wall": (f"{terminal.get('elapsed_wall_seconds', '?')}s wall / {terminal.get('elapsed_agent_seconds', '?')}s agent "
                 f"(approval wait {round(metrics.get('approval_wait_seconds', 0.0), 1)}s)"),
        "tokens": (terminal.get("token_usage") or {}).get("total_tokens", "n/a"),
    }


def table(summaries: list[dict], label: str) -> str:
    header = ("| Scenario | Terminal outcome | LLM requests (by operation) | Tool calls | Actions executed | Plan revisions (triggers) | "
              "High-risk proposed / approved-denied / executed | Recovery evidence; close result | Avoidable calls | "
              "Wall-clock | Tokens |")
    lines = [f"_Source: {label}_", "", header, "|" + "---|" * 11]
    for s in summaries:
        scenario = re.sub(r"\.jsonl$", "", s["trace"])
        lines.append(f"| {scenario} | {s['outcome']} | {s['llm']} | {s['tools']} | {s['actions']} | {s['revisions']} | {s['high_risk']} | "
                     f"{s['recovery']} | {s['avoidable']} | {s['wall']} | {s['tokens']} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--update-report", type=Path, help="replace the EVAL-TABLE block in this markdown file")
    parser.add_argument("--label", default="live Groq runs (traces/*.jsonl), generated by scripts/summarize_trace.py")
    args = parser.parse_args()
    rendered = table([summarise(p) for p in args.traces], args.label)
    print(rendered)
    if args.update_report:
        text = args.update_report.read_text(encoding="utf-8")
        if START not in text or END not in text:
            sys.exit(f"{args.update_report} has no {START} ... {END} block")
        before, rest = text.split(START, 1)
        _, after = rest.split(END, 1)
        args.update_report.write_text(f"{before}{START}\n{rendered}\n{END}{after}", encoding="utf-8")
        print(f"\nUpdated {args.update_report}")


if __name__ == "__main__":
    main()
