"""Configuration loading for the agent runtime.

All budget numbers come from ``configs/limits.json`` and all risk levels from
``configs/risk_policy.json``. Nothing here hard-codes a limit: if a TA edits a config
file (e.g. lowers ``max_llm_calls`` during the viva), the next run picks it up.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_LIMIT_KEYS = (
    "max_llm_calls",
    "max_tool_calls",
    "max_consecutive_model_retries",
    "max_same_action_repeats",
    "max_runtime_seconds",
    "warning_llm_calls_remaining",
)


def resolve_config_path(path: str | Path) -> Path:
    """Prefer the path as given (relative to the CWD, like the starter's RiskPolicy),
    and fall back to the repository root so the agent also works when launched from
    another directory. Both point at the same file in a normal checkout."""
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    from_root = REPO_ROOT / candidate
    return from_root if from_root.exists() else candidate


def load_json_config(path: str | Path) -> dict[str, Any]:
    return json.loads(resolve_config_path(path).read_text(encoding="utf-8"))


def load_limits(path: str | Path = "configs/limits.json") -> dict[str, Any]:
    limits = load_json_config(path)
    missing = [key for key in REQUIRED_LIMIT_KEYS if key not in limits]
    if missing:
        raise ValueError(f"{path} is missing required keys: {missing}")
    return limits


def load_services(path: str | Path = "configs/services.json") -> tuple[list[str], list[str]]:
    data = load_json_config(path)
    return list(data["services"]), list(data.get("critical_path", []))
