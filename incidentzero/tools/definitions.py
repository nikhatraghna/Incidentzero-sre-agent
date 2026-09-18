from __future__ import annotations


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


SERVICE = {"type": "string", "description": "Exact service name from the simulated platform."}
VERSION = {"type": "integer", "minimum": 1, "description": "Latest observed world_version. Used as an optimistic concurrency precondition."}
REASON = {"type": "string", "minLength": 8, "maxLength": 300, "description": "Evidence-based justification for this action."}

TOOLS = [
    _fn("get_incident", "Read the current incident ticket. The ticket may contain unverified hypotheses.", {}, []),
    _fn("get_service_health", "Read health, replica count and version for one service.", {"service": SERVICE}, ["service"]),
    _fn("get_metrics", "Read current operational metrics for one service.", {"service": SERVICE}, ["service"]),
    _fn("get_logs", "Read recent simulated logs for one service.", {"service": SERVICE, "limit": {"type": "integer", "minimum": 1, "maximum": 30}}, ["service", "limit"]),
    _fn("get_deployments", "Read current and previous deployment versions for one service.", {"service": SERVICE}, ["service"]),
    _fn("get_dependencies", "Read direct downstream and upstream dependencies for one service.", {"service": SERVICE}, ["service"]),
    _fn("get_runbook", "Read a short local operational runbook. This is not web access or RAG.", {"topic": {"type": "string", "enum": ["general", "deployment", "capacity", "memory", "database", "cache"]}}, ["topic"]),
    _fn("verify_recovery", "Evaluate objective incident-recovery criteria. Must be used before closure.", {}, []),
    _fn("restart_service", "Restart one service. Use only with evidence that a restart is reasonable.", {"service": SERVICE, "expected_world_version": VERSION, "reason": REASON}, ["service", "expected_world_version", "reason"]),
    _fn("scale_service", "Change replicas for one service.", {"service": SERVICE, "replicas": {"type": "integer", "minimum": 1, "maximum": 8}, "expected_world_version": VERSION, "reason": REASON}, ["service", "replicas", "expected_world_version", "reason"]),
    _fn("clear_cache", "Invalidate redis-cache contents. Can cause a cold-cache penalty.", {"service": {"type": "string", "enum": ["redis-cache"]}, "expected_world_version": VERSION, "reason": REASON}, ["service", "expected_world_version", "reason"]),
    _fn("rollback_deployment", "Rollback a service to its known previous release. High risk; controller approval is required.", {"service": SERVICE, "target_version": {"type": "string"}, "expected_world_version": VERSION, "reason": REASON}, ["service", "target_version", "expected_world_version", "reason"]),
    _fn("failover_database", "Fail over order-db to a healthy replica. Critical risk; controller approval is required.", {"service": {"type": "string", "enum": ["order-db"]}, "expected_world_version": VERSION, "reason": REASON}, ["service", "expected_world_version", "reason"]),
    _fn("shift_traffic", "Shift service traffic to the secondary pool. High risk; controller approval is required.", {"service": SERVICE, "percent_to_secondary": {"type": "integer", "enum": [25, 50, 75, 100]}, "expected_world_version": VERSION, "reason": REASON}, ["service", "percent_to_secondary", "expected_world_version", "reason"]),
    _fn("close_incident", "Close only after verify_recovery reports criteria_met=true and cite its evidence id.", {"summary": {"type": "string", "minLength": 20}, "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "expected_world_version": VERSION, "reason": REASON}, ["summary", "evidence_ids", "expected_world_version", "reason"]),
    _fn("escalate_incident", "Escalate when safe autonomous resolution is not possible or remaining budget is insufficient.", {"reason": {"type": "string", "minLength": 20}, "evidence_ids": {"type": "array", "items": {"type": "string"}},}, ["reason", "evidence_ids"]),
]

TOOL_BY_NAME = {item["function"]["name"]: item for item in TOOLS}
