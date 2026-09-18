from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from .models import IncidentSpec, ServiceState
from .scenario_factory import build_scenario
from .topology import SERVICE_GRAPH, upstream_of


class SimulationEnvironment:
    """Deterministic local production simulator.

    Public agent code should interact through ToolRegistry, not through private fields.
    Hidden grading may replace this class with a clean version.
    """

    def __init__(self, student_id: str, scenario_id: str = "public-a") -> None:
        incident, services, seed = build_scenario(student_id, scenario_id)
        self._scenario_spec: IncidentSpec = incident
        self._services: dict[str, ServiceState] = services
        self._seed = seed
        self._world_version = 1
        self._tool_call_count = 0
        self._evidence_counter = 0
        self._closed = False
        self._escalated = False
        self._latest_verify_evidence: str | None = None
        self._failure_remaining = copy.deepcopy(incident.failure_plan)
        self._event_fired = False

    @property
    def world_version(self) -> int:
        return self._world_version

    @property
    def incident_id(self) -> str:
        return self._scenario_spec.incident_id

    def _evidence_id(self) -> str:
        self._evidence_counter += 1
        return f"EV-{self._evidence_counter:04d}"

    def _result(self, tool: str, status: str = "ok", data: Any = None, **extra: Any) -> dict[str, Any]:
        out = {
            "status": status,
            "tool": tool,
            "world_version": self._world_version,
            "evidence_id": self._evidence_id(),
            "data": data,
        }
        out.update(extra)
        return out

    def _before_tool(self, tool: str) -> dict[str, Any] | None:
        self._tool_call_count += 1
        self._fire_scheduled_event_if_due()
        remaining = self._failure_remaining.get(tool, 0)
        if remaining > 0:
            self._failure_remaining[tool] = remaining - 1
            return self._result(
                tool, "transient_error", None,
                retryable=True,
                message=f"Simulated telemetry backend timeout for {tool}.",
            )
        return None

    def _fire_scheduled_event_if_due(self) -> None:
        spec = self._scenario_spec
        if self._event_fired or spec.event_at_tool_call is None:
            return
        if self._tool_call_count < spec.event_at_tool_call:
            return
        self._event_fired = True
        if spec.event_type == "traffic_shift":
            svc = self._services["checkout-service"]
            svc.cpu_pct = min(100.0, svc.cpu_pct + 12)
            svc.p95_ms += 250
            svc.logs.append("INFO traffic increased by 18% after campaign launch")
        elif spec.event_type == "instance_loss":
            svc = self._services["checkout-service"]
            svc.replicas = max(1, svc.replicas - 1)
            svc.cpu_pct = min(100.0, svc.cpu_pct + 15)
            svc.logs.append("WARN one checkout instance became unavailable")
        self._world_version += 1

    def execute(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        transient = self._before_tool(tool)
        if transient:
            return transient
        fn = getattr(self, f"_tool_{tool}", None)
        if fn is None:
            return self._result(tool, "error", None, retryable=False, message="Unknown tool.")
        return fn(**arguments)

    def _known_service(self, service: str) -> ServiceState | None:
        return self._services.get(service)

    def _tool_get_incident(self) -> dict[str, Any]:
        s = self._scenario_spec
        data = {
            "incident_id": s.incident_id,
            "title": s.title,
            "severity": s.severity,
            "customer_impact": s.customer_impact,
            "suspected_service": s.suspected_service,
            "operator_note": s.misleading_hint,
            "objective": "Restore checkout success >=99%, p95 <=800ms, and critical services to healthy state.",
        }
        return self._result("get_incident", data=data)

    def _tool_get_service_health(self, service: str) -> dict[str, Any]:
        s = self._known_service(service)
        if not s:
            return self._result("get_service_health", "error", None, retryable=False, message="Unknown service.")
        return self._result("get_service_health", data={
            "service": service, "healthy": s.healthy, "replicas": s.replicas, "version": s.version,
        })

    def _tool_get_metrics(self, service: str) -> dict[str, Any]:
        s = self._known_service(service)
        if not s:
            return self._result("get_metrics", "error", None, retryable=False, message="Unknown service.")
        data = {
            "service": service, "error_rate": round(s.error_rate, 4), "p95_ms": s.p95_ms,
            "cpu_pct": round(s.cpu_pct, 1), "memory_pct": round(s.memory_pct, 1),
            "replicas": s.replicas,
        }
        if s.cache_hit_rate is not None: data["cache_hit_rate"] = round(s.cache_hit_rate, 3)
        if s.db_connections_pct is not None: data["db_connections_pct"] = round(s.db_connections_pct, 1)
        return self._result("get_metrics", data=data)

    def _tool_get_logs(self, service: str, limit: int = 10) -> dict[str, Any]:
        s = self._known_service(service)
        if not s:
            return self._result("get_logs", "error", None, retryable=False, message="Unknown service.")
        limit = max(1, min(int(limit), 30))
        baseline = [f"INFO {service} heartbeat ok", f"INFO {service} request sample completed"]
        return self._result("get_logs", data={"service": service, "entries": (baseline + s.logs)[-limit:]})

    def _tool_get_deployments(self, service: str) -> dict[str, Any]:
        s = self._known_service(service)
        if not s:
            return self._result("get_deployments", "error", None, retryable=False, message="Unknown service.")
        return self._result("get_deployments", data={
            "service": service,
            "current": {"version": s.version, "deployed_minutes_ago": 24 if service == "checkout-service" else 410},
            "previous": {"version": s.previous_version, "known_good": True},
        })

    def _tool_get_dependencies(self, service: str) -> dict[str, Any]:
        if service not in SERVICE_GRAPH:
            return self._result("get_dependencies", "error", None, retryable=False, message="Unknown service.")
        return self._result("get_dependencies", data={
            "service": service,
            "depends_on": SERVICE_GRAPH[service],
            "called_by": upstream_of(service),
        })

    def _tool_get_runbook(self, topic: str) -> dict[str, Any]:
        runbooks = {
            "deployment": ["Compare onset with deployment time.", "Prefer rollback to last known-good version when evidence is strong.", "Verify after rollback."],
            "capacity": ["Confirm saturation with CPU/queue evidence.", "Scale only the bottleneck service.", "Verify latency and errors after scaling."],
            "memory": ["Confirm sustained memory pressure and OOM evidence.", "A bounded restart may clear a leak temporarily.", "Escalate if pressure immediately returns."],
            "database": ["Confirm DB I/O/connection saturation.", "Failover is high risk and requires approval.", "Verify dependent services after failover."],
            "cache": ["Look for stale/corrupt object evidence.", "Cache invalidation can be disruptive; verify downstream services."],
            "general": ["Do not trust the incident ticket as root-cause evidence.", "Use observations to test hypotheses.", "Do not close without explicit recovery verification."],
        }
        if topic not in runbooks:
            return self._result("get_runbook", "error", None, retryable=False, message="Unknown runbook topic.")
        return self._result("get_runbook", data={"topic": topic, "steps": runbooks[topic]})

    def _check_version(self, tool: str, expected_world_version: int) -> dict[str, Any] | None:
        if expected_world_version != self._world_version:
            return self._result(
                tool, "stale_precondition", None, retryable=True,
                message="World changed after your observation. Re-observe before taking this action.",
                expected=expected_world_version, actual=self._world_version,
            )
        return None

    def _tool_restart_service(self, service: str, expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("restart_service", expected_world_version)
        if stale: return stale
        s = self._known_service(service)
        if not s: return self._result("restart_service", "error", None, retryable=False, message="Unknown service.")
        if self._scenario_spec.family == "memory_leak" and service == self._scenario_spec.target_service:
            s.memory_pct = 48; s.error_rate = 0.008; s.p95_ms = 310; s.healthy = True
            effect = "memory pressure cleared and service recovered"
        else:
            s.p95_ms = max(180, s.p95_ms - 100)
            effect = "service restarted; no confirmed root-cause remediation"
        self._world_version += 1
        return self._result("restart_service", data={"service": service, "effect": effect, "reason": reason})

    def _tool_scale_service(self, service: str, replicas: int, expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("scale_service", expected_world_version)
        if stale: return stale
        s = self._known_service(service)
        if not s: return self._result("scale_service", "error", None, retryable=False, message="Unknown service.")
        if replicas < 1 or replicas > 8:
            return self._result("scale_service", "error", None, retryable=False, message="Replicas must be 1..8.")
        old = s.replicas; s.replicas = replicas
        if self._scenario_spec.family == "capacity_spike" and service == self._scenario_spec.target_service and replicas >= 4:
            s.cpu_pct = 46; s.error_rate = 0.006; s.p95_ms = 420; s.healthy = True
            effect = "capacity bottleneck relieved"
        else:
            s.cpu_pct = max(25, s.cpu_pct - max(0, replicas-old)*8)
            effect = "replica count changed; root cause may remain"
        self._world_version += 1
        return self._result("scale_service", data={"service": service, "old_replicas": old, "replicas": replicas, "effect": effect, "reason": reason})

    def _tool_clear_cache(self, service: str, expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("clear_cache", expected_world_version)
        if stale: return stale
        if service != "redis-cache":
            return self._result("clear_cache", "error", None, retryable=False, message="Only redis-cache can be cleared.")
        s = self._services[service]
        if self._scenario_spec.family == "cache_corruption":
            s.error_rate = 0.003; s.p95_ms = 220; s.healthy = True; s.cache_hit_rate = 0.84
            cart = self._services["cart-service"]; cart.error_rate = 0.004; cart.p95_ms = 260; cart.healthy = True
            effect = "corrupt cache objects removed; cache will warm gradually"
        else:
            s.cache_hit_rate = 0.60; s.p95_ms += 220
            effect = "cache cleared without evidence of corruption; temporary cold-cache penalty"
        self._world_version += 1
        return self._result("clear_cache", data={"service": service, "effect": effect, "reason": reason})

    def _tool_rollback_deployment(self, service: str, target_version: str, expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("rollback_deployment", expected_world_version)
        if stale: return stale
        s = self._known_service(service)
        if not s: return self._result("rollback_deployment", "error", None, retryable=False, message="Unknown service.")
        if target_version != s.previous_version:
            return self._result("rollback_deployment", "error", None, retryable=False, message="target_version is not the known previous release.")
        s.version = target_version
        if self._scenario_spec.family == "bad_deploy" and service == self._scenario_spec.target_service:
            s.error_rate = 0.004; s.p95_ms = 360; s.healthy = True
            effect = "known-bad release removed"
        else:
            effect = "rollback completed but incident symptoms remain"
        self._world_version += 1
        return self._result("rollback_deployment", data={"service": service, "version": target_version, "effect": effect, "reason": reason})

    def _tool_failover_database(self, service: str, expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("failover_database", expected_world_version)
        if stale: return stale
        if service != "order-db":
            return self._result("failover_database", "error", None, retryable=False, message="Only order-db supports failover.")
        db = self._services[service]
        if self._scenario_spec.family == "database_primary_degraded":
            db.db_connections_pct = 38; db.cpu_pct = 41; db.error_rate = 0.002; db.p95_ms = 240; db.healthy = True
            order = self._services["order-service"]; order.error_rate = 0.004; order.p95_ms = 330; order.healthy = True
            effect = "traffic moved to healthy database replica"
        else:
            effect = "failover completed; no evidence it addressed the incident"
        self._world_version += 1
        return self._result("failover_database", data={"service": service, "effect": effect, "reason": reason})

    def _tool_shift_traffic(self, service: str, percent_to_secondary: int, expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("shift_traffic", expected_world_version)
        if stale: return stale
        if service not in self._services:
            return self._result("shift_traffic", "error", None, retryable=False, message="Unknown service.")
        if percent_to_secondary not in {25, 50, 75, 100}:
            return self._result("shift_traffic", "error", None, retryable=False, message="Allowed percentages: 25, 50, 75, 100.")
        self._world_version += 1
        return self._result("shift_traffic", data={"service": service, "percent_to_secondary": percent_to_secondary, "effect": "traffic shifted; verify outcome", "reason": reason})

    def _aggregate_checkout_health(self) -> dict[str, Any]:
        # Evaluate the customer path plus the incident-specific component. This prevents a
        # local action from being counted as success while an incident-relevant dependency
        # remains unhealthy.
        names = {"checkout-service", "payment-service", "inventory-service", "order-service", "order-db"}
        names.add(self._scenario_spec.target_service)
        if self._scenario_spec.family == "cache_corruption":
            names.update({"cart-service", "redis-cache"})
        critical = [self._services[x] for x in sorted(names)]
        error = max(x.error_rate for x in critical)
        latency = max(x.p95_ms for x in critical)
        all_healthy = all(x.healthy for x in critical)
        success_rate = max(0.0, 1.0 - error)
        return {
            "checkout_success_rate": round(success_rate, 4),
            "critical_path_p95_ms": latency,
            "critical_services_healthy": all_healthy,
            "evaluated_components": [x.name for x in critical],
            "criteria_met": success_rate >= 0.99 and latency <= 800 and all_healthy,
        }

    def _tool_verify_recovery(self) -> dict[str, Any]:
        data = self._aggregate_checkout_health()
        result = self._result("verify_recovery", data=data)
        self._latest_verify_evidence = result["evidence_id"]
        return result

    def _tool_close_incident(self, summary: str, evidence_ids: list[str], expected_world_version: int, reason: str) -> dict[str, Any]:
        stale = self._check_version("close_incident", expected_world_version)
        if stale: return stale
        health = self._aggregate_checkout_health()
        if not health["criteria_met"]:
            return self._result("close_incident", "error", health, retryable=False, message="Recovery criteria are not met.")
        if not self._latest_verify_evidence or self._latest_verify_evidence not in evidence_ids:
            return self._result("close_incident", "error", health, retryable=False, message="Latest verify_recovery evidence must be cited.")
        self._closed = True; self._world_version += 1
        return self._result("close_incident", data={"closed": True, "summary": summary, "evidence_ids": evidence_ids, "reason": reason})

    def _tool_escalate_incident(self, reason: str, evidence_ids: list[str]) -> dict[str, Any]:
        self._escalated = True; self._world_version += 1
        return self._result("escalate_incident", data={"escalated": True, "reason": reason, "evidence_ids": evidence_ids})

    # Instructor/diagnostic only. Student agents must never use this method.
    def _oracle_snapshot(self) -> dict[str, Any]:
        return {
            "family": self._scenario_spec.family,
            "target_service": self._scenario_spec.target_service,
            "impossible": self._scenario_spec.impossible,
            "world_version": self._world_version,
            "closed": self._closed,
            "escalated": self._escalated,
        }
