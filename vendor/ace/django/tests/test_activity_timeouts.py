"""Tests for activity timeout enforcement.

Tests verify:
- Schedule-to-close timeout detection
- Start-to-close timeout detection
- Heartbeat timeout detection
- Retry vs fail decisions
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz
from uuid import UUID

import pytest
from django.db import transaction

from ace import WorkflowStatus
from ace_django.activity_timeouts import DjangoActivityTimeoutService, TimeoutResult
from ace_django.models import (
    ActivityAttempt,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkflowRun,
)

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


def create_activity(
    workflow: WorkflowRun,
    activity_key: str = "test-activity",
    status: str = ActivityStatus.RUNNING,
    schedule_to_close_at: datetime | None = None,
    start_to_close_seconds: float | None = None,
    heartbeat_seconds: float | None = None,
) -> ActivityRun:
    """Create an activity run."""
    return ActivityRun.objects.create(
        workflow_run=workflow,
        activity_key=activity_key,
        activity_name="test.activity",
        status=status,
        current_attempt=1,
        schedule_to_close_at=schedule_to_close_at,
        start_to_close_seconds=start_to_close_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )


def create_attempt(
    activity: ActivityRun,
    started_at: datetime | None = None,
    heartbeat_deadline: datetime | None = None,
) -> ActivityAttempt:
    """Create an activity attempt."""
    started = started_at or FROZEN_NOW
    return ActivityAttempt.objects.create(
        activity_run=activity,
        attempt_number=activity.current_attempt,
        worker_id="worker-1",
        lease_expires_at=started + timedelta(minutes=5),
        heartbeat_at=started,
        started_at=started,
        heartbeat_deadline=heartbeat_deadline,
    )


def test_no_timeouts_returns_none() -> None:
    """enforce_once returns None when no timeouts are due."""
    service = DjangoActivityTimeoutService()
    result = service.enforce_once(now=FROZEN_NOW)
    assert result is None


def test_schedule_to_close_timeout_detected() -> None:
    """enforce_once detects schedule_to_close timeout."""
    workflow = create_workflow_run()
    activity = create_activity(
        workflow,
        schedule_to_close_at=FROZEN_NOW - timedelta(seconds=1),
    )

    service = DjangoActivityTimeoutService()
    result = service.enforce_once(now=FROZEN_NOW)

    assert result is not None
    assert result.timeout_type == "schedule_to_close"
    assert result.failed is True

    # Verify activity is failed
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.FAILED


def test_schedule_to_close_not_due() -> None:
    """enforce_once skips schedule_to_close not yet due."""
    workflow = create_workflow_run()
    create_activity(
        workflow,
        schedule_to_close_at=FROZEN_NOW + timedelta(hours=1),
    )

    service = DjangoActivityTimeoutService()
    result = service.enforce_once(now=FROZEN_NOW)

    assert result is None


def test_terminal_workflow_skips_timeout() -> None:
    """enforce_once skips activities in terminal workflows."""
    workflow = create_workflow_run(status=WorkflowStatus.COMPLETED.value)
    create_activity(
        workflow,
        schedule_to_close_at=FROZEN_NOW - timedelta(seconds=1),
    )

    service = DjangoActivityTimeoutService()
    result = service.enforce_once(now=FROZEN_NOW)

    assert result is None


def test_heartbeat_timeout_detected() -> None:
    """enforce_once detects heartbeat timeout."""
    workflow = create_workflow_run()
    activity = create_activity(
        workflow,
        heartbeat_seconds=30.0,
    )
    create_attempt(
        activity,
        started_at=FROZEN_NOW - timedelta(minutes=1),
        heartbeat_deadline=FROZEN_NOW - timedelta(seconds=1),
    )

    service = DjangoActivityTimeoutService()
    result = service.enforce_once(now=FROZEN_NOW)

    assert result is not None
    assert result.timeout_type == "heartbeat"
