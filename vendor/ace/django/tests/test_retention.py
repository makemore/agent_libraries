"""Tests for workflow history retention (purgeacehistory).

Tests verify:
- Dry-run by default
- Only terminal workflows purged
- Min age requirement
- BLOCKED/unprocessed excluded
- Batch processing
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command

from ace import WorkflowStatus
from ace_django.models import (
    InboxStatus,
    TimerStatus,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTimer,
)

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=tz.utc)


def create_workflow(
    status: str,
    completed_at: datetime | None = None,
    namespace: str = "default",
) -> WorkflowRun:
    """Create a workflow run for testing."""
    return WorkflowRun.objects.create(
        workflow_name="test-workflow",
        workflow_version="1",
        status=status,
        state={},
        input={},
        idempotency_key=f"test-{uuid4()}",
        started_at=FROZEN_NOW,
        completed_at=completed_at,
        namespace=namespace,
    )


def test_purge_dry_run_by_default() -> None:
    """purgeacehistory is dry-run by default."""
    run = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
    )

    out = StringIO()
    call_command("purgeacehistory", "--min-age-days=7", stdout=out)

    # Run should still exist
    assert WorkflowRun.objects.filter(pk=run.pk).exists()
    assert "DRY RUN" in out.getvalue()


def test_purge_requires_min_age() -> None:
    """purgeacehistory requires --min-age-days."""
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("purgeacehistory")


def test_purge_deletes_old_terminal() -> None:
    """purgeacehistory deletes old terminal workflows with --confirm."""
    old_completed = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
    )
    old_failed = create_workflow(
        WorkflowStatus.FAILED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
    )

    out = StringIO()
    call_command("purgeacehistory", "--min-age-days=7", "--confirm", stdout=out)

    assert not WorkflowRun.objects.filter(pk=old_completed.pk).exists()
    assert not WorkflowRun.objects.filter(pk=old_failed.pk).exists()
    assert "2 workflows" in out.getvalue()


def test_purge_respects_min_age() -> None:
    """purgeacehistory respects minimum age."""
    from django.utils import timezone

    now = timezone.now()

    recent = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=now - timedelta(days=3),
    )
    old = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=now - timedelta(days=30),
    )

    call_command("purgeacehistory", "--min-age-days=7", "--confirm")

    assert WorkflowRun.objects.filter(pk=recent.pk).exists()
    assert not WorkflowRun.objects.filter(pk=old.pk).exists()


def test_purge_excludes_running() -> None:
    """purgeacehistory excludes RUNNING workflows."""
    running = create_workflow(WorkflowStatus.RUNNING.value)

    call_command("purgeacehistory", "--min-age-days=1", "--confirm")

    assert WorkflowRun.objects.filter(pk=running.pk).exists()


def test_purge_excludes_blocked() -> None:
    """purgeacehistory excludes BLOCKED workflows."""
    blocked = create_workflow(
        WorkflowStatus.BLOCKED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
    )

    call_command("purgeacehistory", "--min-age-days=1", "--confirm")

    assert WorkflowRun.objects.filter(pk=blocked.pk).exists()


def test_purge_excludes_with_pending_inbox() -> None:
    """purgeacehistory excludes workflows with pending inbox events."""
    run = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
    )
    WorkflowInboxEvent.objects.create(
        workflow_run=run,
        inbox_sequence=1,
        source_type="activity",
        source_key="test-activity",
        event_type="activity.completed",
        status=InboxStatus.PENDING,
        occurred_at=FROZEN_NOW,
    )

    call_command("purgeacehistory", "--min-age-days=1", "--confirm")

    assert WorkflowRun.objects.filter(pk=run.pk).exists()


def test_purge_excludes_with_dlq_timer() -> None:
    """purgeacehistory excludes workflows with DLQ timers."""
    run = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
    )
    WorkflowTimer.objects.create(
        workflow_run=run,
        timer_key="test-timer",
        fire_at=FROZEN_NOW,
        status=TimerStatus.DEAD_LETTER,
    )

    call_command("purgeacehistory", "--min-age-days=1", "--confirm")

    assert WorkflowRun.objects.filter(pk=run.pk).exists()


def test_purge_namespace_filter() -> None:
    """purgeacehistory respects --namespace filter."""
    ns1_run = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
        namespace="ns1",
    )
    ns2_run = create_workflow(
        WorkflowStatus.COMPLETED.value,
        completed_at=FROZEN_NOW - timedelta(days=30),
        namespace="ns2",
    )

    call_command("purgeacehistory", "--min-age-days=1", "--namespace=ns1", "--confirm")

    assert not WorkflowRun.objects.filter(pk=ns1_run.pk).exists()
    assert WorkflowRun.objects.filter(pk=ns2_run.pk).exists()
