"""Tests for queue configuration and QoS.

Tests verify:
- QueueConfig model basics
- syncacequeues management command
- Queue-based activity materialization
"""

from __future__ import annotations

from datetime import datetime, timezone as tz
from io import StringIO
from uuid import UUID, uuid4

import pytest
from django.core.management import call_command

from ace_django.models import QueueConfig

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=tz.utc)


def test_queue_config_creation() -> None:
    """QueueConfig can be created with defaults."""
    queue = QueueConfig.objects.create(name="test-queue")

    assert queue.name == "test-queue"
    assert queue.enabled is True
    assert queue.global_concurrency is None
    assert queue.rate_limit_count is None
    assert queue.partition_concurrency is None


def test_queue_config_with_limits() -> None:
    """QueueConfig can be created with limits."""
    queue = QueueConfig.objects.create(
        name="limited-queue",
        enabled=True,
        global_concurrency=10,
        rate_limit_count=100,
        rate_limit_period_seconds=60,
        partition_concurrency=2,
    )

    assert queue.global_concurrency == 10
    assert queue.rate_limit_count == 100
    assert queue.rate_limit_period_seconds == 60
    assert queue.partition_concurrency == 2


def test_queue_config_unique_name() -> None:
    """QueueConfig enforces unique names."""
    QueueConfig.objects.create(name="unique-queue")

    with pytest.raises(Exception):  # IntegrityError
        QueueConfig.objects.create(name="unique-queue")


def test_syncacequeues_creates_default_queue() -> None:
    """syncacequeues creates default medium queue."""
    out = StringIO()
    call_command("syncacequeues", stdout=out)

    assert QueueConfig.objects.filter(name="medium").exists()
    output = out.getvalue()
    assert "created" in output.lower() or "sync complete" in output.lower()


def test_syncacequeues_idempotent() -> None:
    """syncacequeues is idempotent."""
    call_command("syncacequeues")
    call_command("syncacequeues")

    # Should only have one medium queue
    assert QueueConfig.objects.filter(name="medium").count() == 1


def test_syncacequeues_dry_run() -> None:
    """syncacequeues --dry-run doesn't create queues."""
    # Delete any existing medium queue to start clean
    QueueConfig.objects.filter(name="medium").delete()

    out = StringIO()
    call_command("syncacequeues", "--dry-run", stdout=out)

    # Should not actually create
    assert not QueueConfig.objects.filter(name="medium").exists()
    assert "dry run" in out.getvalue().lower()


def test_syncacequeues_disable_unlisted(settings) -> None:
    """syncacequeues --disable-unlisted disables extra queues."""
    # Create an extra queue
    extra = QueueConfig.objects.create(name="extra-queue", enabled=True)

    # Sync with disable-unlisted
    out = StringIO()
    call_command("syncacequeues", "--disable-unlisted", stdout=out)

    extra.refresh_from_db()
    assert extra.enabled is False
    assert "disabled" in out.getvalue().lower()


# QoS enforcement tests


def create_workflow_and_activity(
    queue: str = "medium",
    partition_key: str | None = None,
    priority: int = 0,
) -> tuple:
    """Create a workflow with a single activity."""
    from ace_django.models import ActivityRun, ActivityStatus, WorkflowRun

    run = WorkflowRun.objects.create(
        workflow_name="test-workflow",
        workflow_version="1",
        status="RUNNING",
        state={},
        input={},
        idempotency_key=f"test-{uuid4()}",
        started_at=FROZEN_NOW,
    )

    activity = ActivityRun.objects.create(
        workflow_run=run,
        activity_key="test-activity",
        activity_name="test.activity",
        queue=queue,
        partition_key=partition_key,
        priority=priority,
        status=ActivityStatus.READY,
    )

    return run, activity


def test_claim_respects_global_concurrency_limit() -> None:
    """claim skips queues that have reached global concurrency limit."""
    from ace_django.models import ActivityRun, ActivityStatus
    from ace_django.queue import DjangoActivityQueue

    # Create a queue with global concurrency of 1
    QueueConfig.objects.create(
        name="limited",
        enabled=True,
        global_concurrency=1,
    )

    # Create standalone activities (no workflow)
    activity1 = ActivityRun.objects.create(
        workflow_run=None,
        activity_key="test-activity-1",
        activity_name="test.activity",
        queue="limited",
        status=ActivityStatus.READY,
    )
    activity2 = ActivityRun.objects.create(
        workflow_run=None,
        activity_key="test-activity-2",
        activity_name="test.activity",
        queue="limited",
        status=ActivityStatus.READY,
    )

    queue = DjangoActivityQueue()

    # First claim should succeed
    lease1 = queue.claim("worker-1", queues=("limited",), now=FROZEN_NOW)
    assert lease1 is not None

    # Second claim should return None (at limit)
    lease2 = queue.claim("worker-2", queues=("limited",), now=FROZEN_NOW)
    assert lease2 is None

    # Complete the first activity
    queue.complete(lease1, {"result": "done"}, now=FROZEN_NOW)

    # Now second claim should succeed
    lease3 = queue.claim("worker-2", queues=("limited",), now=FROZEN_NOW)
    assert lease3 is not None


def test_claim_respects_disabled_queue() -> None:
    """claim skips disabled queues."""
    from ace_django.queue import DjangoActivityQueue

    # Create a disabled queue
    QueueConfig.objects.create(
        name="disabled-queue",
        enabled=False,
    )

    run, activity = create_workflow_and_activity(queue="disabled-queue")

    queue = DjangoActivityQueue()
    lease = queue.claim("worker-1", queues=("disabled-queue",), now=FROZEN_NOW)

    assert lease is None


def test_claim_respects_partition_concurrency() -> None:
    """claim respects per-partition concurrency limits."""
    from ace_django.queue import DjangoActivityQueue

    # Create a queue with partition concurrency of 1
    QueueConfig.objects.create(
        name="partitioned",
        enabled=True,
        partition_concurrency=1,
    )

    # Create two activities in the same partition
    run1, activity1 = create_workflow_and_activity(
        queue="partitioned",
        partition_key="user-123",
    )
    run2, activity2 = create_workflow_and_activity(
        queue="partitioned",
        partition_key="user-123",
    )
    # And one in a different partition
    run3, activity3 = create_workflow_and_activity(
        queue="partitioned",
        partition_key="user-456",
    )

    queue = DjangoActivityQueue()

    # First claim for partition user-123 should succeed
    lease1 = queue.claim("worker-1", queues=("partitioned",), now=FROZEN_NOW)
    assert lease1 is not None
    assert lease1.activity_run_id == str(activity1.pk)

    # Second claim should get from user-456 partition (user-123 is at limit)
    lease2 = queue.claim("worker-2", queues=("partitioned",), now=FROZEN_NOW)
    assert lease2 is not None
    assert lease2.activity_run_id == str(activity3.pk)

    # Third claim should return None (both partitions at limit)
    lease3 = queue.claim("worker-3", queues=("partitioned",), now=FROZEN_NOW)
    assert lease3 is None


def test_claim_orders_by_priority() -> None:
    """claim returns higher priority activities first."""
    from ace_django.models import ActivityRun, ActivityStatus
    from ace_django.queue import DjangoActivityQueue

    # Create standalone activities with different priorities
    activity_low = ActivityRun.objects.create(
        workflow_run=None,
        activity_key="test-activity-low",
        activity_name="test.activity",
        queue="medium",
        priority=0,
        status=ActivityStatus.READY,
    )
    activity_high = ActivityRun.objects.create(
        workflow_run=None,
        activity_key="test-activity-high",
        activity_name="test.activity",
        queue="medium",
        priority=10,
        status=ActivityStatus.READY,
    )
    activity_medium = ActivityRun.objects.create(
        workflow_run=None,
        activity_key="test-activity-medium",
        activity_name="test.activity",
        queue="medium",
        priority=5,
        status=ActivityStatus.READY,
    )

    queue = DjangoActivityQueue()

    # First claim should get highest priority
    lease1 = queue.claim("worker-1", queues=("medium",), now=FROZEN_NOW)
    assert lease1.activity_run_id == str(activity_high.pk)

    # Complete and get next
    queue.complete(lease1, {}, now=FROZEN_NOW)

    lease2 = queue.claim("worker-2", queues=("medium",), now=FROZEN_NOW)
    assert lease2.activity_run_id == str(activity_medium.pk)

    queue.complete(lease2, {}, now=FROZEN_NOW)

    lease3 = queue.claim("worker-3", queues=("medium",), now=FROZEN_NOW)
    assert lease3.activity_run_id == str(activity_low.pk)
