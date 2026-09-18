import pytest

from incidentzero.environment.engine import SimulationEnvironment
from incidentzero.environment.scenario_factory import stable_seed
from incidentzero.telemetry.budget import BudgetExceeded, BudgetManager


@pytest.mark.infrastructure
def test_student_seed_is_deterministic():
    assert stable_seed("22I-1234", "public-a") == stable_seed("22I-1234", "public-a")
    assert stable_seed("22I-1234", "public-a") != stable_seed("22I-1235", "public-a")


@pytest.mark.infrastructure
def test_tool_results_have_world_version_and_evidence(registry):
    result = registry.execute("get_incident", {})
    assert result["status"] == "ok"
    assert isinstance(result["world_version"], int)
    assert result["evidence_id"].startswith("EV-")


@pytest.mark.infrastructure
def test_stale_action_is_rejected(env, registry):
    first = registry.execute("get_incident", {})
    observed_version = first["world_version"]
    # First mutate the world using the observed version, then deliberately reuse the old version.
    registry.execute("restart_service", {
        "service": "auth-service", "expected_world_version": observed_version,
        "reason": "Create a newer world version for the concurrency test.",
    })
    result = registry.execute("restart_service", {
        "service": "checkout-service", "expected_world_version": observed_version,
        "reason": "Testing optimistic concurrency precondition after state changed.",
    })
    assert result["status"] == "stale_precondition"


@pytest.mark.infrastructure
def test_budget_manager_enforces_limits():
    b = BudgetManager(max_llm_calls=1, max_tool_calls=1)
    b.consume_llm(); b.consume_tool()
    with pytest.raises(BudgetExceeded): b.consume_llm()
    with pytest.raises(BudgetExceeded): b.consume_tool()
