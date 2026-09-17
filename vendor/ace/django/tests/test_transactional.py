"""Tests for transactional activity execution.

Tests verify:
- Transactional executor atomicity
- Rollback on exception
- Lock ordering
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz
from uuid import UUID, uuid4

import pytest

from ace import WorkflowStatus
from ace_django.models import (
    ActivityAttempt,
    ActivityExecutionMode,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkflowRun,
)
from ace_django.queue import ActivityLease
from ace_django.transactional import DjangoTransactionalActivityExecutor, TransactionalResult

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


def create_transactional_activity(
    workflow: WorkflowRun,
    activity_key: str = "tx-activity",
) -> tuple[ActivityRun, ActivityAttempt, ActivityLease]:
    """Create a transactional activity with lease."""
    ownership_token = uuid4()

    activity = ActivityRun.objects.create(
        workflow_run=workflow,
        activity_key=activity_key,
        activity_name="test.transactional",
        activity_version="1",
        status=ActivityStatus.RUNNING,
        execution_mode=ActivityExecutionMode.TRANSACTIONAL,
        current_attempt=1,
    )

    attempt = ActivityAttempt.objects.create(
        activity_run=activity,
        attempt_number=1,
        worker_id="worker-1",
        ownership_token=ownership_token,
        lease_expires_at=FROZEN_NOW + timedelta(minutes=5),
        heartbeat_at=FROZEN_NOW,
        started_at=FROZEN_NOW,
    )

    lease = ActivityLease(
        activity_run_id=str(activity.pk),
        workflow_run_id=str(workflow.pk),
        activity_key=activity_key,
        activity_name="test.transactional",
        activity_version="1",
        input={},
        attempt=1,
        worker_id="worker-1",
        ownership_token=str(ownership_token),
        lease_expires_at=attempt.lease_expires_at,
    )

    return activity, attempt, lease


def test_transactional_success() -> None:
    """Transactional execution commits callable and completion together."""
    workflow = create_workflow_run()
    activity, attempt, lease = create_transactional_activity(workflow)

    executor = DjangoTransactionalActivityExecutor()

    def callable() -> dict:
        return {"result": "success"}

    result = executor.execute(lease, callable, now=FROZEN_NOW)

    assert result.success is True
    assert result.result == {"result": "success"}

    # Verify activity completed
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.SUCCEEDED
    assert activity.result == {"result": "success"}


def test_transactional_exception_rollback() -> None:
    """Transactional execution rolls back on exception."""
    workflow = create_workflow_run()
    activity, attempt, lease = create_transactional_activity(workflow)

    executor = DjangoTransactionalActivityExecutor()

    def failing_callable() -> dict:
        raise ValueError("Test failure")

    result = executor.execute(lease, failing_callable, now=FROZEN_NOW)

    assert result.success is False
    assert "Test failure" in (result.error or "")

    # Verify activity NOT completed (rolled back)
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.RUNNING


def test_transactional_rejects_non_transactional_mode() -> None:
    """Transactional executor rejects non-TRANSACTIONAL activities."""
    workflow = create_workflow_run()
    ownership_token = uuid4()

    # Create STANDARD mode activity
    activity = ActivityRun.objects.create(
        workflow_run=workflow,
        activity_key="standard-activity",
        activity_name="test.standard",
        status=ActivityStatus.RUNNING,
        execution_mode=ActivityExecutionMode.STANDARD,  # Not transactional
        current_attempt=1,
    )

    ActivityAttempt.objects.create(
        activity_run=activity,
        attempt_number=1,
        worker_id="worker-1",
        ownership_token=ownership_token,
        lease_expires_at=FROZEN_NOW + timedelta(minutes=5),
        heartbeat_at=FROZEN_NOW,
        started_at=FROZEN_NOW,
    )

    lease = ActivityLease(
        activity_run_id=str(activity.pk),
        workflow_run_id=str(workflow.pk),
        activity_key="standard-activity",
        activity_name="test.standard",
        activity_version="1",
        input={},
        attempt=1,
        worker_id="worker-1",
        ownership_token=str(ownership_token),
        lease_expires_at=FROZEN_NOW + timedelta(minutes=5),
    )

    executor = DjangoTransactionalActivityExecutor()
    result = executor.execute(lease, lambda: "result", now=FROZEN_NOW)

    assert result.success is False
    assert "not TRANSACTIONAL" in (result.error or "")
