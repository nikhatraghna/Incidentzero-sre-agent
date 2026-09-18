from __future__ import annotations

import hashlib
import random

from .models import IncidentSpec, ServiceState


FAMILIES = [
    "bad_deploy",
    "memory_leak",
    "capacity_spike",
    "database_primary_degraded",
    "cache_corruption",
]


def stable_seed(student_id: str, scenario_id: str) -> int:
    raw = hashlib.sha256(f"{student_id.strip().upper()}::{scenario_id}".encode()).digest()
    return int.from_bytes(raw[:8], "big")


def _base_services() -> dict[str, ServiceState]:
    names = [
        "edge-gateway", "auth-service", "catalog-service", "cart-service",
        "checkout-service", "payment-service", "inventory-service", "order-service",
        "redis-cache", "order-db",
    ]
    services = {name: ServiceState(name=name) for name in names}
    services["checkout-service"].version = "2.4.1"
    services["checkout-service"].previous_version = "2.4.0"
    services["inventory-service"].version = "3.8.2"
    services["inventory-service"].previous_version = "3.8.1"
    services["payment-service"].version = "5.1.0"
    services["payment-service"].previous_version = "5.0.3"
    services["redis-cache"].cache_hit_rate = 0.94
    services["order-db"].db_connections_pct = 44.0
    return services


def build_scenario(student_id: str, scenario_id: str = "public-a") -> tuple[IncidentSpec, dict[str, ServiceState], int]:
    seed = stable_seed(student_id, scenario_id)
    rng = random.Random(seed)
    services = _base_services()

    families = list(FAMILIES)
    if scenario_id.startswith("hidden"):
        families.append("external_dependency")
    family = families[seed % len(families)]

    target = {
        "bad_deploy": "checkout-service",
        "memory_leak": "inventory-service",
        "capacity_spike": "checkout-service",
        "database_primary_degraded": "order-db",
        "cache_corruption": "redis-cache",
        "external_dependency": "payment-service",
    }[family]

    suspected = rng.choice(["checkout-service", "payment-service", "order-service", target])
    misleading = rng.choice([
        "Alert correlation tentatively points to checkout-service.",
        "A dashboard note suggests a recent traffic increase may be involved.",
        "First responder suspected payment-service, but this is unverified.",
        "The incident ticket contains no confirmed root cause.",
    ])

    if family == "bad_deploy":
        s = services[target]
        s.healthy = False; s.error_rate = 0.19; s.p95_ms = 2850; s.cpu_pct = 58
        s.logs += ["ERROR Null state in checkout rule evaluator", "WARN failures began after rollout 2.4.1"]
        title = "Checkout errors increased after a production change"
    elif family == "memory_leak":
        s = services[target]
        s.healthy = False; s.error_rate = 0.11; s.p95_ms = 1950; s.memory_pct = 97
        s.logs += ["WARN heap pressure 96%", "ERROR worker killed by OOM guard", "WARN allocation rate rising"]
        title = "Inventory requests are timing out intermittently"
    elif family == "capacity_spike":
        s = services[target]
        s.healthy = False; s.error_rate = 0.08; s.p95_ms = 2200; s.cpu_pct = 99; s.replicas = 2
        s.logs += ["WARN request queue depth=940", "WARN worker saturation detected"]
        title = "Checkout latency is above SLO during traffic surge"
    elif family == "database_primary_degraded":
        s = services[target]
        s.healthy = False; s.error_rate = 0.06; s.p95_ms = 3200; s.cpu_pct = 92; s.db_connections_pct = 96
        s.logs += ["WARN primary I/O latency 180ms", "ERROR connection acquisition timeout"]
        services["order-service"].p95_ms = 2300; services["order-service"].error_rate = 0.07
        title = "Order writes are timing out across the checkout path"
    elif family == "cache_corruption":
        s = services[target]
        s.healthy = False; s.error_rate = 0.14; s.p95_ms = 800; s.cache_hit_rate = 0.99
        s.logs += ["ERROR checksum mismatch for cart session objects", "WARN stale schema objects served"]
        services["cart-service"].error_rate = 0.10; services["cart-service"].p95_ms = 1100
        title = "Cart sessions show inconsistent state after cache schema migration"
    else:
        s = services[target]
        s.healthy = False; s.error_rate = 0.21; s.p95_ms = 3400
        s.logs += ["ERROR upstream PSP returned 503", "WARN retry budget exhausted"]
        title = "Payment authorization failures originate outside the managed platform"

    failure_plan = {}
    if (seed >> 8) % 3 == 0:
        failure_plan["get_logs"] = 1
    if (seed >> 16) % 4 == 0:
        failure_plan["get_metrics"] = 1

    event_type = rng.choice(["traffic_shift", "instance_loss", "none"])
    event_at = rng.choice([4, 5, 6]) if event_type != "none" else None

    incident = IncidentSpec(
        incident_id=f"INC-{str(seed)[-6:]}",
        title=title,
        customer_impact="Checkout completion rate is below the 99% service objective; customer-facing errors are active.",
        severity=rng.choice(["SEV-1", "SEV-2"]),
        suspected_service=suspected,
        misleading_hint=misleading,
        family=family,
        target_service=target,
        failure_plan=failure_plan,
        event_at_tool_call=event_at,
        event_type=None if event_type == "none" else event_type,
        impossible=(family == "external_dependency"),
        parameters={"seed_fingerprint": hashlib.sha256(str(seed).encode()).hexdigest()[:8]},
    )
    return incident, services, seed
