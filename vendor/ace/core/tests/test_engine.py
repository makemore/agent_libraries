from concurrent.futures import ThreadPoolExecutor

import pytest
from ace import (
    CompleteWorkflow,
    IdempotencyConflict,
    InMemoryExecutionStore,
    InvalidClock,
    InvalidTransition,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowEngine,
    WorkflowEventType,
    WorkflowRegistry,
    WorkflowStatus,
)

from tests.helpers import (
    DrainingWorkflow,
    ExampleWorkflow,
    FixedClock,
    NaiveClock,
    SequenceIds,
    ThreadSafeIds,
    build_engine,
    build_group_engine,
)


def test_workflow_advances_without_history_replay() -> None:
    engine, store = build_engine()

    started = engine.start("example", {"subject": "risk-1"}, idempotency_key="intake-1")
    waiting = engine.handle_event(started.run_id, WorkflowEventType.ACTIVITY_COMPLETED)
    completed = engine.handle_event(waiting.run_id, WorkflowEventType.TIMER_FIRED)

    assert started.status == WorkflowStatus.RUNNING
    assert waiting.status == WorkflowStatus.WAITING
    assert completed.status == WorkflowStatus.COMPLETED
    assert completed.result == {"subject": "risk-1"}
    assert [event.sequence for event in store.history(started.run_id)] == [1, 2, 3]
    commands = store.commands(started.run_id)
    assert isinstance(commands[0], ScheduleActivity)
    assert isinstance(commands[1], ScheduleTimer)
    assert isinstance(commands[2], CompleteWorkflow)


def test_idempotent_start_returns_existing_run() -> None:
    engine, store = build_engine()

    first = engine.start("example", {"subject": "risk-1"}, idempotency_key="intake-1")
    second = engine.start("example", {"subject": "risk-1"}, idempotency_key="intake-1")

    assert second == first
    assert len(store.history(first.run_id)) == 1
    assert len(store.commands(first.run_id)) == 1
    engine.handle_event(first.run_id, WorkflowEventType.ACTIVITY_COMPLETED)
    assert store.history(first.run_id)[-1].event_id == "event-2"


def test_concurrent_idempotent_starts_create_one_run() -> None:
    registry = WorkflowRegistry()
    registry.register(ExampleWorkflow())
    store = InMemoryExecutionStore()
    engine = WorkflowEngine(registry, store, FixedClock(), ThreadSafeIds())

    def start() -> str:
        return engine.start(
            "example",
            {"subject": "risk-1"},
            idempotency_key="intake-1",
        ).run_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        run_ids = tuple(executor.map(lambda _index: start(), range(8)))

    assert len(set(run_ids)) == 1
    assert len(store.history(run_ids[0])) == 1


def test_idempotency_key_rejects_different_input() -> None:
    engine, _store = build_engine()
    engine.start("example", {"subject": "risk-1"}, idempotency_key="intake-1")

    with pytest.raises(IdempotencyConflict, match="different input or version"):
        engine.start("example", {"subject": "risk-2"}, idempotency_key="intake-1")


def test_cancellation_is_an_explicit_workflow_event() -> None:
    engine, store = build_engine()
    started = engine.start("example", {"subject": "risk-1"})

    cancelled = engine.request_cancellation(started.run_id, reason="operator request")

    assert cancelled.status == WorkflowStatus.CANCELLED
    assert cancelled.result == {"reason": "operator request"}
    assert store.history(started.run_id)[-1].event_type == "workflow.cancellation_requested"


def test_cancellation_can_drain_running_activity_before_terminal_state() -> None:
    registry = WorkflowRegistry()
    registry.register(DrainingWorkflow())
    store = InMemoryExecutionStore()
    engine = WorkflowEngine(
        registry,
        store,
        FixedClock(),
        SequenceIds("run-1", "event-1", "event-2", "event-3"),
    )
    started = engine.start("draining", {})

    draining = engine.request_cancellation(started.run_id, reason="operator request")
    cancelled = engine.handle_event(started.run_id, WorkflowEventType.ACTIVITY_COMPLETED)

    assert draining.status == WorkflowStatus.CANCELLING
    assert cancelled.status == WorkflowStatus.CANCELLED


def test_terminal_workflow_rejects_more_events() -> None:
    engine, _store = build_engine()
    started = engine.start("example", {"subject": "risk-1"})
    completed = engine.handle_event(started.run_id, WorkflowEventType.TIMER_FIRED)

    with pytest.raises(InvalidTransition, match="already terminal"):
        engine.handle_event(completed.run_id, "unexpected")


def test_engine_rejects_naive_clock() -> None:
    registry = WorkflowRegistry()
    registry.register(ExampleWorkflow())
    engine = WorkflowEngine(
        registry,
        InMemoryExecutionStore(),
        NaiveClock(),
        SequenceIds("run-1", "event-1"),
    )

    with pytest.raises(InvalidClock, match="timezone-aware"):
        engine.start("example", {"subject": "risk-1"})


# ---------------------------------------------------------------------------
# Activity group engine tests
# ---------------------------------------------------------------------------


def test_group_workflow_schedules_group_and_advances_on_completed() -> None:
    engine, store = build_group_engine("run-1", "event-1", "event-2")

    started = engine.start("group-example", {})
    assert started.status == WorkflowStatus.RUNNING

    # The store should have received a ScheduleActivityGroup command.
    commands = store.commands(started.run_id)
    assert len(commands) == 1
    assert isinstance(commands[0], ScheduleActivityGroup)
    assert commands[0].group_key == "parallel-extract"
    assert len(commands[0].activities) == 2

    # Simulate group completion event.
    completed = engine.handle_event(
        started.run_id,
        WorkflowEventType.ACTIVITY_GROUP_COMPLETED,
        payload={
            "group_key": "parallel-extract",
            "group_run_id": "grp-run-1",
            "results": {"extract-a": {"part": "a"}, "extract-b": {"part": "b"}},
        },
    )
    assert completed.status == WorkflowStatus.COMPLETED
    assert completed.result == {"extract-a": {"part": "a"}, "extract-b": {"part": "b"}}


def test_group_workflow_handles_group_failure() -> None:
    engine, _store = build_group_engine("run-1", "event-1", "event-2")

    started = engine.start("group-example", {})

    failed = engine.handle_event(
        started.run_id,
        WorkflowEventType.ACTIVITY_GROUP_FAILED,
        payload={
            "group_key": "parallel-extract",
            "group_run_id": "grp-run-1",
            "results": {"extract-a": {"part": "a"}},
            "failed_activity_key": "extract-b",
            "failed_activity_run_id": "act-run-b",
            "failure": {"error_type": "ExtractError", "message": "bad input"},
        },
    )
    assert failed.status == WorkflowStatus.COMPLETED
    assert failed.result == {"partial": {"extract-a": {"part": "a"}}}


def test_group_counts_as_runnable_work_for_running_status() -> None:
    engine, _store = build_group_engine("run-1", "event-1")

    started = engine.start("group-example", {})
    # GroupWorkflow.start() emits only a ScheduleActivityGroup, no standalone
    # ScheduleActivity — status should still be RUNNING.
    assert started.status == WorkflowStatus.RUNNING


def test_group_event_replay_produces_identical_state() -> None:
    """Replaying the same group event against the same state yields identical results."""
    engine1, _store1 = build_group_engine("run-1", "event-1", "event-2")
    engine2, _store2 = build_group_engine("run-1", "event-1", "event-2")

    payload = {
        "group_key": "parallel-extract",
        "group_run_id": "grp-run-1",
        "results": {"extract-a": {"part": "a"}, "extract-b": {"part": "b"}},
    }

    engine1.start("group-example", {})
    result1 = engine1.handle_event(
        "run-1", WorkflowEventType.ACTIVITY_GROUP_COMPLETED, payload=payload
    )

    engine2.start("group-example", {})
    result2 = engine2.handle_event(
        "run-1", WorkflowEventType.ACTIVITY_GROUP_COMPLETED, payload=payload
    )

    assert result1.state == result2.state
    assert result1.result == result2.result
    assert result1.status == result2.status
