"""Tests for the durable inbox system.

Tests verify:
- Atomic event enqueuing with proper sequencing
- Duplicate detection based on source_type/source_key
- Idempotent re-enqueue with same payload
- Conflict detection for different payloads
- Inbox ordering and retrieval
"""

from __future__ import annotations

from datetime import datetime, timezone as tz
from uuid import UUID

import pytest
from django.db import transaction
from django.utils import timezone

from ace import WorkflowEventType
from ace_django.exceptions import DuplicateInboxEvent
from ace_django.inbox import (
    discard_remaining_inbox,
    enqueue_locked,
    get_next_pending_inbox,
    mark_dead_letter,
    mark_discarded,
    mark_processed,
    mark_retrying,
)
from ace_django.models import InboxStatus, WorkflowInboxEvent, WorkflowRun

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=tz.utc)


def create_workflow_run(run_id: str = "10000000-0000-0000-0000-000000000001") -> WorkflowRun:
    """Create a minimal workflow run for testing."""
    return WorkflowRun.objects.create(
        id=UUID(run_id),
        workflow_name="test-workflow",
        workflow_version="1",
        status="RUNNING",
        state={},
        input={"test": True},
        idempotency_key=f"test-{run_id}",
        started_at=FROZEN_NOW,
    )


def test_enqueue_locked_creates_inbox_event() -> None:
    """enqueue_locked creates an inbox event with proper sequencing."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox = enqueue_locked(
            locked_run,
            source_type="activity",
            source_key="do_work",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"result": "ok"},
            occurred_at=FROZEN_NOW,
        )

    assert inbox.inbox_sequence == 1
    assert inbox.status == InboxStatus.PENDING
    assert inbox.source_type == "activity"
    assert inbox.source_key == "do_work"
    assert inbox.payload == {"result": "ok"}


def test_enqueue_locked_increments_sequence() -> None:
    """Multiple enqueues get sequential inbox_sequence values."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox1 = enqueue_locked(
            locked_run,
            source_type="activity",
            source_key="step1",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"r": 1},
            occurred_at=FROZEN_NOW,
        )
        inbox2 = enqueue_locked(
            locked_run,
            source_type="activity",
            source_key="step2",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"r": 2},
            occurred_at=FROZEN_NOW,
        )
        inbox3 = enqueue_locked(
            locked_run,
            source_type="timer",
            source_key="deadline",
            event_type=WorkflowEventType.TIMER_FIRED,
            payload={},
            occurred_at=FROZEN_NOW,
        )

    assert inbox1.inbox_sequence == 1
    assert inbox2.inbox_sequence == 2
    assert inbox3.inbox_sequence == 3


def test_enqueue_locked_idempotent_same_payload() -> None:
    """Re-enqueue with same source and payload returns existing event."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox1 = enqueue_locked(
            locked_run,
            source_type="activity",
            source_key="step1",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"result": 42},
            occurred_at=FROZEN_NOW,
        )
        inbox2 = enqueue_locked(
            locked_run,
            source_type="activity",
            source_key="step1",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"result": 42},
            occurred_at=FROZEN_NOW,
        )

    assert inbox1.pk == inbox2.pk
    assert WorkflowInboxEvent.objects.filter(workflow_run=run).count() == 1


def test_enqueue_locked_rejects_different_payload() -> None:
    """Re-enqueue with same source but different payload raises."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        enqueue_locked(
            locked_run,
            source_type="activity",
            source_key="step1",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"result": 42},
            occurred_at=FROZEN_NOW,
        )

        with pytest.raises(DuplicateInboxEvent):
            enqueue_locked(
                locked_run,
                source_type="activity",
                source_key="step1",
                event_type=WorkflowEventType.ACTIVITY_COMPLETED,
                payload={"result": 99},
                occurred_at=FROZEN_NOW,
            )


def test_get_next_pending_inbox_returns_lowest_sequence() -> None:
    """get_next_pending_inbox returns the lowest sequence pending event."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        enqueue_locked(
            locked_run,
            source_type="a",
            source_key="1",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={},
            occurred_at=FROZEN_NOW,
        )
        enqueue_locked(
            locked_run,
            source_type="a",
            source_key="2",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={},
            occurred_at=FROZEN_NOW,
        )
        enqueue_locked(
            locked_run,
            source_type="a",
            source_key="3",
            event_type=WorkflowEventType.ACTIVITY_COMPLETED,
            payload={},
            occurred_at=FROZEN_NOW,
        )

    next_event = get_next_pending_inbox(run)
    assert next_event is not None
    assert next_event.inbox_sequence == 1


def _enqueue(run: WorkflowRun, source_type: str, source_key: str) -> WorkflowInboxEvent:
    """Helper to enqueue with keyword args."""
    return enqueue_locked(
        run,
        source_type=source_type,
        source_key=source_key,
        event_type=WorkflowEventType.ACTIVITY_COMPLETED,
        payload={},
        occurred_at=FROZEN_NOW,
    )


def test_mark_processed_updates_status() -> None:
    """mark_processed transitions event to PROCESSED."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox = _enqueue(locked_run, "a", "1")

    mark_processed(inbox, now=FROZEN_NOW)

    inbox.refresh_from_db()
    assert inbox.status == InboxStatus.PROCESSED
    assert inbox.processed_at == FROZEN_NOW


def test_mark_retrying_schedules_retry_with_backoff() -> None:
    """mark_retrying increments attempts and sets available_at."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox = _enqueue(locked_run, "a", "1")

    mark_retrying(
        inbox,
        error_type="TransitionError",
        error_message="Something went wrong",
        retry_delay_seconds=10.0,
        now=FROZEN_NOW,
    )

    inbox.refresh_from_db()
    assert inbox.status == InboxStatus.RETRYING
    assert inbox.attempts == 1
    assert inbox.last_error_type == "TransitionError"
    # available_at should be 10 seconds after FROZEN_NOW
    from datetime import timedelta

    expected_available = FROZEN_NOW + timedelta(seconds=10)
    assert inbox.available_at == expected_available


def test_mark_dead_letter_after_max_retries() -> None:
    """mark_dead_letter transitions to DEAD_LETTER status."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox = _enqueue(locked_run, "a", "1")

    mark_dead_letter(
        inbox,
        error_type="MaxRetriesExceeded",
        error_message="Failed after 5 attempts",
        now=FROZEN_NOW,
    )

    inbox.refresh_from_db()
    assert inbox.status == InboxStatus.DEAD_LETTER
    assert inbox.dead_letter_at == FROZEN_NOW


def test_mark_discarded_for_terminal_workflow() -> None:
    """mark_discarded transitions to DISCARDED status."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        inbox = _enqueue(locked_run, "a", "1")

    mark_discarded(inbox, reason="Workflow completed", now=FROZEN_NOW)

    inbox.refresh_from_db()
    assert inbox.status == InboxStatus.DISCARDED
    assert inbox.discard_reason == "Workflow completed"


def test_discard_remaining_inbox_for_terminal_workflow() -> None:
    """discard_remaining_inbox discards all pending events."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        _enqueue(locked_run, "a", "1")
        _enqueue(locked_run, "a", "2")
        _enqueue(locked_run, "a", "3")

    # Mark one as processed
    first = WorkflowInboxEvent.objects.get(workflow_run=run, inbox_sequence=1)
    mark_processed(first, now=FROZEN_NOW)

    # Discard remaining
    count = discard_remaining_inbox(run, "Workflow cancelled", now=FROZEN_NOW)

    assert count == 2  # Two events discarded

    # Verify states
    events = list(WorkflowInboxEvent.objects.filter(workflow_run=run).order_by("inbox_sequence"))
    assert events[0].status == InboxStatus.PROCESSED  # Not touched
    assert events[1].status == InboxStatus.DISCARDED
    assert events[2].status == InboxStatus.DISCARDED


def test_get_next_pending_inbox_skips_processed() -> None:
    """get_next_pending_inbox skips already processed events."""
    run = create_workflow_run()

    with transaction.atomic():
        locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
        _enqueue(locked_run, "a", "1")
        _enqueue(locked_run, "a", "2")

    # Process the first one
    first = WorkflowInboxEvent.objects.get(workflow_run=run, inbox_sequence=1)
    mark_processed(first, now=FROZEN_NOW)

    next_event = get_next_pending_inbox(run)
    assert next_event is not None
    assert next_event.inbox_sequence == 2


def test_get_next_pending_inbox_returns_none_when_empty() -> None:
    """get_next_pending_inbox returns None when no pending events."""
    run = create_workflow_run()

    next_event = get_next_pending_inbox(run)
    assert next_event is None
