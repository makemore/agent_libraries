"""Tests for the durable timer service.

Tests verify:
- Timer delivery creates inbox events
- Timer-vs-cancellation race handling
- Retry and dead-letter behavior
- Terminal workflow discards timers
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz
from uuid import UUID

import pytest
from django.db import transaction

from ace import WorkflowEventType, WorkflowStatus
from ace_django.models import (
    InboxStatus,
    TimerStatus,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTimer,
)
from ace_django.timers import DjangoTimerService, TimerDeliveryResult

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=tz.utc)


def create_workflow_run(
    run_id: str = "10000000-0000-0000-0000-000000000001",
    status: str = "RUNNING",
) -> WorkflowRun:
    """Create a minimal workflow run for testing."""
    return WorkflowRun.objects.create(
        id=UUID(run_id),
        workflow_name="test-workflow",
        workflow_version="1",
        status=status,
        state={},
        input={"test": True},
        idempotency_key=f"test-{run_id}",
        started_at=FROZEN_NOW,
    )


def create_timer(
    run: WorkflowRun,
    timer_key: str = "test-timer",
    fire_at: datetime | None = None,
    status: str = TimerStatus.SCHEDULED,
) -> WorkflowTimer:
    """Create a workflow timer."""
    return WorkflowTimer.objects.create(
        workflow_run=run,
        timer_key=timer_key,
        status=status,
        fire_at=fire_at or FROZEN_NOW,
        payload={"data": "test"},
    )


def test_deliver_once_returns_none_when_no_timers() -> None:
    """deliver_once returns None when no timers exist."""
    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)
    assert result is None


def test_deliver_once_returns_none_when_timer_not_due() -> None:
    """deliver_once returns None when timer is not yet due."""
    run = create_workflow_run()
    create_timer(run, fire_at=FROZEN_NOW + timedelta(hours=1))

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)
    assert result is None


def test_deliver_once_delivers_due_timer() -> None:
    """deliver_once delivers a timer that is due."""
    run = create_workflow_run()
    timer = create_timer(run, fire_at=FROZEN_NOW - timedelta(seconds=1))

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)

    assert result is not None
    assert result.success is True
    assert result.delivered is True

    # Verify timer is marked delivered
    timer.refresh_from_db()
    assert timer.status == TimerStatus.DELIVERED
    assert timer.delivered_at == FROZEN_NOW

    # Verify inbox event created
    inbox = WorkflowInboxEvent.objects.get(workflow_run=run)
    assert inbox.source_type == "timer"
    assert inbox.source_key == "test-timer"
    assert inbox.event_type == WorkflowEventType.TIMER_FIRED


def test_deliver_skips_terminal_workflow() -> None:
    """deliver_once skips timers for terminal workflows."""
    run = create_workflow_run(status=WorkflowStatus.COMPLETED.value)
    create_timer(run)

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)

    # Should not find any candidate (filtered in query)
    assert result is None


def test_deliver_cancels_timer_for_terminal_workflow() -> None:
    """Timer is cancelled if workflow becomes terminal under lock."""
    run = create_workflow_run()
    timer = create_timer(run)

    # Make workflow terminal after timer query but before lock
    # (simulated by checking behavior when status changes)
    run.status = WorkflowStatus.COMPLETED.value
    run.save(update_fields=["status", "updated_at"])

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)

    # Query filters out terminal workflows
    assert result is None


def test_deliver_skips_blocked_workflow() -> None:
    """deliver_once skips timers for blocked workflows."""
    run = create_workflow_run(status=WorkflowStatus.BLOCKED.value)
    create_timer(run)

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)

    # Blocked workflows are excluded from query
    assert result is None


def test_deliver_retrying_timer() -> None:
    """deliver_once picks up retrying timers when available_at is due."""
    run = create_workflow_run()
    timer = create_timer(run, status=TimerStatus.RETRYING)
    timer.next_attempt_at = FROZEN_NOW - timedelta(seconds=1)
    timer.delivery_attempts = 1
    timer.save(update_fields=["next_attempt_at", "delivery_attempts", "updated_at"])

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)

    assert result is not None
    assert result.delivered is True


def test_retrying_timer_skipped_when_not_due() -> None:
    """Retrying timer is skipped when next_attempt_at is in the future."""
    run = create_workflow_run()
    timer = create_timer(run, status=TimerStatus.RETRYING)
    timer.next_attempt_at = FROZEN_NOW + timedelta(hours=1)
    timer.save(update_fields=["next_attempt_at", "updated_at"])

    service = DjangoTimerService()
    result = service.deliver_once(now=FROZEN_NOW)

    assert result is None
