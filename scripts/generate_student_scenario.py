from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.tools.registry import ToolRegistry


p = argparse.ArgumentParser()
p.add_argument("--student-id", required=True)
p.add_argument("--scenario", default="public-a")
args = p.parse_args()

env = SimulationEnvironment(args.student_id, args.scenario)
registry = ToolRegistry(env)
incident = registry.execute("get_incident", {})
print(json.dumps({
    "student_id": args.student_id,
    "scenario": args.scenario,
    "incident": incident,
    "note": "Root cause is intentionally not printed. Investigate through tools.",
}, indent=2))
