import pytest
from ace import (
    ActivityCancelled,
    ActivityContext,
    ActivityRegistry,
    DefinitionNotFound,
    DuplicateDefinition,
    InvalidAttempt,
    InvalidPolicy,
    RetryPolicy,
    WorkflowRegistry,
)
from ace.json_types import JsonObject, JsonValue

from tests.helpers import ExampleWorkflow


def sample_activity(context: ActivityContext, input: JsonObject) -> JsonValue:
    context.heartbeat({"stage": "running"})
    context.check_cancelled()
    return input


def test_registries_require_explicit_unique_registration() -> None:
    workflows = WorkflowRegistry()
    activities = ActivityRegistry()
    definition = ExampleWorkflow()

    assert workflows.register(definition) is definition
    assert activities.register("example.extract", sample_activity) is sample_activity
    assert workflows.resolve("example", "1") is definition
    assert activities.resolve("example.extract") is sample_activity

    with pytest.raises(DuplicateDefinition, match="registered twice"):
        workflows.register(definition)
    with pytest.raises(DuplicateDefinition, match="registered twice"):
        activities.register("example.extract", sample_activity)
    with pytest.raises(DefinitionNotFound, match="not registered"):
        activities.resolve("missing")


def test_retry_policy_has_bounded_exponential_backoff() -> None:
    policy = RetryPolicy(
        max_attempts=5,
        initial_delay_seconds=2,
        backoff_multiplier=3,
        max_delay_seconds=20,
    )

    assert [policy.delay_after(attempt) for attempt in range(1, 6)] == [2, 6, 18, 20, 20]


def test_retry_policy_rejects_invalid_values() -> None:
    with pytest.raises(InvalidPolicy, match="at least 1"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(InvalidPolicy, match="cannot be negative"):
        RetryPolicy(initial_delay_seconds=-1)
    with pytest.raises(InvalidAttempt, match="failed_attempt"):
        RetryPolicy().delay_after(0)
    with pytest.raises(InvalidPolicy, match="jitter_fraction"):
        RetryPolicy(jitter_fraction=1.5)
    with pytest.raises(InvalidPolicy, match="jitter_fraction"):
        RetryPolicy(jitter_fraction=-0.1)


def test_retry_policy_jitter_defaults_to_off() -> None:
    """jitter_fraction=0.0 (the default) is byte-for-byte deterministic even
    with a "random" rng, so pre-jitter callers see no behavior change."""
    policy = RetryPolicy(initial_delay_seconds=2, backoff_multiplier=3, max_delay_seconds=20)
    assert policy.delay_after(1, rng=lambda: 0.0) == 2
    assert policy.delay_after(1, rng=lambda: 1.0) == 2


def test_retry_policy_jitter_spreads_delay_within_fraction() -> None:
    policy = RetryPolicy(
        initial_delay_seconds=10,
        backoff_multiplier=1,
        max_delay_seconds=10,
        jitter_fraction=0.2,
    )
    # rng() == 0.0 -> minimum end of the jitter window (-fraction).
    assert policy.delay_after(1, rng=lambda: 0.0) == pytest.approx(8.0)
    # rng() == 1.0 -> maximum end of the jitter window (+fraction), clamped
    # to max_delay_seconds.
    assert policy.delay_after(1, rng=lambda: 1.0) == pytest.approx(10.0)
    # rng() == 0.5 -> no shift.
    assert policy.delay_after(1, rng=lambda: 0.5) == pytest.approx(10.0)


def test_retry_policy_jitter_never_goes_negative() -> None:
    policy = RetryPolicy(
        initial_delay_seconds=1,
        backoff_multiplier=1,
        max_delay_seconds=1,
        jitter_fraction=1.0,
    )
    assert policy.delay_after(1, rng=lambda: 0.0) == 0.0


def test_activity_context_raises_named_cancellation() -> None:
    context = ActivityContext(
        activity_run_id="activity-1",
        operation_id="operation-1",
        workflow_run_id="workflow-1",
        attempt=2,
        heartbeat=lambda _details: None,
        cancellation_requested=lambda: True,
    )

    with pytest.raises(ActivityCancelled, match="activity-1"):
        context.check_cancelled()
