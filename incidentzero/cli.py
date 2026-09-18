from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from rich import print

from incidentzero.agent.config import load_limits
from incidentzero.agent.controller import AgentController
from incidentzero.approval.gateway import AlwaysDenyGateway, ConsoleApprovalGateway
from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.model.groq_client import GroqModelClient
from incidentzero.telemetry.budget import BudgetManager
from incidentzero.telemetry.trace import TraceRecorder
from incidentzero.tools.registry import ToolRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incidentzero")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--student-id", required=True)
    run.add_argument("--scenario", default="public-a")
    run.add_argument("--model", default=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"))
    run.add_argument("--approval", choices=["console", "deny"], default="console",
                     help="console: a human types APPROVE for high/critical actions (default); "
                          "deny: every high/critical action is denied (to exercise the denial path).")
    run.add_argument("--limits", default="configs/limits.json", help="budget configuration file")
    run.add_argument("--trace-dir", default="traces")
    return parser


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    if args.command == "run":
        if not os.getenv("GROQ_API_KEY"):
            print("GROQ_API_KEY is not set. Put it in .env (see .env.example) or export it in the shell.")
            sys.exit(2)
        limits = load_limits(args.limits)
        env = SimulationEnvironment(args.student_id, args.scenario)
        registry = ToolRegistry(env)
        trace_path = Path(args.trace_dir) / f"{args.student_id}_{args.scenario}.jsonl"
        approval = ConsoleApprovalGateway() if args.approval == "console" else AlwaysDenyGateway()
        controller = AgentController(
            model=GroqModelClient(model=args.model),
            tools=registry,
            approval=approval,
            budget=BudgetManager.from_limits(limits),
            trace=TraceRecorder(trace_path),
            limits=limits,
        )
        outcome = controller.run()
        print("\n[bold]Outcome[/bold]")
        print(json.dumps(asdict(outcome), indent=2, default=str))
        print(f"Trace written to {trace_path}")


if __name__ == "__main__":
    main()
