import pytest

from incidentzero.agent.policies import LoopGuard, ReplanPolicy
from incidentzero.agent.recovery import RetryPolicy
from incidentzero.model.errors import PermanentModelError, TransientModelError


@pytest.mark.student
def test_replan_policy_recognizes_stale_world():
    policy = ReplanPolicy()
    assert policy.should_replan({"status": "stale_precondition", "retryable": True}) is True


@pytest.mark.student
def test_replan_policy_recognizes_approval_denial():
    policy = ReplanPolicy()
    assert policy.should_replan({"status": "approval_denied", "retryable": False}) is True


@pytest.mark.student
def test_loop_guard_detects_exact_repeat():
    guard = LoopGuard(max_same_action_repeats=2)
    args = {"service": "checkout-service", "replicas": 4}
    assert guard.record("scale_service", args) is False
    assert guard.record("scale_service", args) is False
    assert guard.record("scale_service", args) is True


@pytest.mark.student
def test_retry_policy_retries_transient_only():
    calls = {"n": 0}
    sleeps = []
    retry = RetryPolicy(max_attempts=3, sleeper=lambda s: sleeps.append(s))

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientModelError("429")
        return "ok"

    assert retry.call_model(flaky) == "ok"
    assert calls["n"] == 3

    def permanent():
        raise PermanentModelError("bad request")

    with pytest.raises(PermanentModelError):
        retry.call_model(permanent)
