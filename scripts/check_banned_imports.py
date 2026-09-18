from __future__ import annotations

import ast
from pathlib import Path

BANNED_PREFIXES = {
    "langchain", "langgraph", "crewai", "autogen", "llama_index", "llamaindex",
    "pydantic_ai", "smolagents", "agno", "semantic_kernel", "openai_agents",
    "google.adk", "strands", "beeai",
}

violations = []
for path in Path("incidentzero").rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import): names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module: names = [node.module]
        for name in names:
            if any(name == p or name.startswith(p + ".") for p in BANNED_PREFIXES):
                violations.append((str(path), node.lineno, name))

if violations:
    print("BANNED AGENT-FRAMEWORK IMPORTS FOUND:")
    for row in violations: print(f"  {row[0]}:{row[1]} -> {row[2]}")
    raise SystemExit(2)
print("No banned agent-framework imports found.")
