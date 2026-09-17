import pytest
from ace import IdempotencyConflict, InvalidTransition, WorkflowEventType, WorkflowStatus
from ace_django.models import (
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    TimerStatus,
    WorkflowEvent,
    WorkflowRun,
)
from ace_django.tests.helpers import (
    FROZEN_NOW,
    RUN_ID,
    SECOND_EVENT_ID,
    START_EVENT_ID,
    THIRD_EVENT_ID,
    build_draining_group_engine,
    build_engine,
    build_group_engine,
)

pytestmark = pytest.mark.django_db


def test_start_persists_history_and_materializes_activity() -> None:
    engine = build_engine()

    started = engine.start(
        "adapter-test",
        {"subject": "risk-1"},
        idempotency_key="intake-1",
    )
    duplicate = engine.start(
        "adapter-test",
        {"subject": "risk-1"},
        idempotency_key="intake-1",
    )

    assert duplicate.run_id == started.run_id
    assert WorkflowRun.objects.count() == 1
    assert WorkflowEvent.objects.values_list("sequence", flat=True).get() == 1
    activity = ActivityRun.objects.get()
    assert activity.activity_key == "work"
    assert activity.status == ActivityStatus.READY
    assert activity.retry_policy["max_attempts"] == 2


def test_idempotency_conflict_rejects_different_input() -> None:
    engine = build_engine()
    engine.start("adapter-test", {"subject": "risk-1"}, idempotency_key="intake-1")

    with pytest.raises(IdempotencyConflict, match="different input or version"):
        engine.start("adapter-test", {"subject": "risk-2"}, idempotency_key="intake-1")


def test_event_commit_updates_snapshot_and_materializes_timer() -> None:
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})

    waiting = engine.handle_event(
        started.run_id,
        WorkflowEventType.ACTIVITY_COMPLETED,
        payload={"result": {"ok": True}},
    )

    run = WorkflowRun.objects.get(pk=started.run_id)
    assert waiting.status == WorkflowStatus.WAITING
    assert run.status == WorkflowStatus.WAITING
    assert run.last_event_sequence == 2
    assert list(run.events.values_list("sequence", flat=True)) == [1, 2]
    timer = run.timers.get()
    assert timer.timer_key == "follow-up"
    assert timer.status == TimerStatus.SCHEDULED


def test_cancellation_cancels_unclaimed_work_and_timers() -> None:
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})
    engine.handle_event(started.run_id, WorkflowEventType.ACTIVITY_COMPLETED)

    cancelled = engine.request_cancellation(started.run_id, reason="operator request")

    run = WorkflowRun.objects.get(pk=started.run_id)
    assert cancelled.status == WorkflowStatus.CANCELLED
    assert run.cancel_requested_at == FROZEN_NOW
    assert run.activities.get().status == ActivityStatus.CANCELLED
    assert run.activities.get().updated_at == FROZEN_NOW
    assert run.timers.get().status == TimerStatus.CANCELLED
    assert run.timers.get().cancelled_at == FROZEN_NOW
    assert run.timers.get().updated_at == FROZEN_NOW


def test_timer_event_completes_workflow() -> None:
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})
    engine.handle_event(started.run_id, WorkflowEventType.ACTIVITY_COMPLETED)

    completed = engine.handle_event(started.run_id, WorkflowEventType.TIMER_FIRED)

    assert completed.status == WorkflowStatus.COMPLETED
    assert completed.result == {"ok": True}
    assert WorkflowRun.objects.get(pk=started.run_id).completed_at == FROZEN_NOW


# ---------------------------------------------------------------------------
# Activity group materialization
# ---------------------------------------------------------------------------


def test_group_materializes_atomically_with_members() -> None:
    engine = build_group_engine(RUN_ID, START_EVENT_ID)

    started = engine.start("group-adapter-test", {})
    assert started.status == WorkflowStatus.RUNNING

    # One group created.
    groups = list(ActivityGroupRun.objects.filter(workflow_run_id=started.run_id))
    assert len(groups) == 1
    group = groups[0]
    assert group.group_key == "parallel-extract"
    assert group.completion_policy == "ALL_SUCCESS"
    assert group.status == ActivityGroupStatus.RUNNING

    # Two members created with group FK.
    members = list(
        ActivityRun.objects.filter(workflow_run_id=started.run_id).order_by("activity_key")
    )
    assert len(members) == 2
    assert members[0].activity_key == "extract-a"
    assert members[0].group_id == group.pk
    assert members[1].activity_key == "extract-b"
    assert members[1].group_id == group.pk


def test_group_duplicate_group_key_rolls_back() -> None:
    """Duplicate group key causes IntegrityError, rolls back atomically."""
    engine = build_group_engine(RUN_ID, START_EVENT_ID)
    engine.start("group-adapter-test", {})

    # Manually create a second group with same key — should fail.
    run = WorkflowRun.objects.get(pk=RUN_ID)
    from django.db import IntegrityError

    with pytest.raises(IntegrityError):
        ActivityGroupRun.objects.create(
            workflow_run=run,
            group_key="parallel-extract",
            status=ActivityGroupStatus.RUNNING,
        )


def test_cancellation_cancels_groups_and_unclaimed_members() -> None:
    engine = build_group_engine(RUN_ID, START_EVENT_ID, SECOND_EVENT_ID)

    started = engine.start("group-adapter-test", {})
    engine.request_cancellation(started.run_id, reason="operator request")

    group = ActivityGroupRun.objects.get(workflow_run_id=started.run_id)
    assert group.status == ActivityGroupStatus.CANCELLED
    assert group.completed_at == FROZEN_NOW

    members = ActivityRun.objects.filter(workflow_run_id=started.run_id)
    for member in members:
        assert member.status == ActivityStatus.CANCELLED
        assert member.completed_at == FROZEN_NOW
        assert member.available_at is None


def test_standalone_compatibility_preserved_alongside_groups() -> None:
    """Standalone activities still work when groups are in the schema."""
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})

    activity = ActivityRun.objects.get(workflow_run_id=started.run_id)
    assert activity.group_id is None
    assert activity.activity_key == "work"
    assert ActivityGroupRun.objects.filter(workflow_run_id=started.run_id).count() == 0


def test_draining_cancellation_rejected_while_group_in_flight() -> None:
    """A workflow may not answer cancellation with CANCELLING while a group is
    RUNNING — grouped members emit no per-member events, so the drain would
    never complete. The store rejects the transition loudly."""
    engine = build_draining_group_engine(RUN_ID, START_EVENT_ID, SECOND_EVENT_ID)
    started = engine.start("draining-group-test", {})

    with pytest.raises(InvalidTransition, match="cannot drain"):
        engine.request_cancellation(started.run_id, reason="operator request")

    # The rejected transition rolled back: group and members are untouched.
    run = WorkflowRun.objects.get(pk=started.run_id)
    assert run.status == WorkflowStatus.RUNNING
    assert run.cancel_requested_at is None
    group = ActivityGroupRun.objects.get(workflow_run_id=started.run_id)
    assert group.status == ActivityGroupStatus.RUNNING
