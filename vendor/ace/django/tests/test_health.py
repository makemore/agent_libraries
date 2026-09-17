import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from ace import WorkflowStatus
from ace_django.health import (
    collect_ace_health,
    collect_activity_group_reconciliation,
    collect_queue_readiness,
)
from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityAttempt,
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkerStatus,
    WorkflowRun,
)
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def test_health_reports_queue_age_failures_expired_leases_and_stale_workers() -> None:
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000101",
        workflow_name="tests.health",
        workflow_version="1",
        status=WorkflowStatus.FAILED,
    )
    activity = ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000101",
        workflow_run=run,
        activity_key="health",
        activity_name="tests.health",
        queue="ace-smoke",
        status=ActivityStatus.FAILED,
        available_at=NOW - timedelta(minutes=10),
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000102",
        activity_key="ready-health",
        activity_name="tests.health.ready",
        queue="ace-smoke",
        status=ActivityStatus.READY,
        available_at=NOW - timedelta(minutes=10),
    )
    ActivityAttempt.objects.create(
        id="30000000-0000-0000-0000-000000000101",
        activity_run=activity,
        attempt_number=1,
        status=AttemptStatus.RUNNING,
        worker_id="stale-worker",
        lease_expires_at=NOW - timedelta(seconds=1),
        heartbeat_at=NOW - timedelta(minutes=5),
        started_at=NOW - timedelta(minutes=10),
    )
    AceWorkerHeartbeat.objects.create(
        id="40000000-0000-0000-0000-000000000101",
        worker_id="stale-worker",
        process_id=101,
        hostname="test-host",
        queues=["ace-smoke"],
        status=WorkerStatus.ACTIVE,
        last_seen_at=NOW - timedelta(minutes=5),
    )

    report = collect_ace_health(
        expected_queues=("ace-smoke",),
        heartbeat_stale_seconds=60,
        oldest_ready_seconds=300,
        workers_expected=True,
        now=NOW,
    )

    assert report.healthy is False
    assert report.expired_leases == 1
    assert report.failed_workflows == 1
    assert report.failed_activities == 1
    assert report.stale_heartbeats == 1
    assert report.queues[0].depth == 1
    assert report.queues[0].oldest_ready_age_seconds == 600
    assert "oldest_ready:ace-smoke" in report.unhealthy_reasons
    assert "missing_worker:ace-smoke" in report.unhealthy_reasons


@override_settings(
    ACE_EXPECTED_QUEUES=("ace-smoke",),
    ACE_WORKERS_EXPECTED=True,
    ACE_HEARTBEAT_STALE_SECONDS=60,
    ACE_OLDEST_READY_SECONDS=300,
)
def test_checkacehealth_fails_when_expected_worker_is_missing() -> None:
    output = StringIO()

    with pytest.raises(CommandError, match="unhealthy execution state"):
        call_command("checkacehealth", "--fail-on-unhealthy", stdout=output)

    result = json.loads(output.getvalue())
    assert result["healthy"] is False
    assert result["unhealthy_reasons"] == ["missing_worker:ace-smoke"]


def test_queue_readiness_healthy_with_fresh_worker() -> None:
    AceWorkerHeartbeat.objects.create(
        id="40000000-0000-0000-0000-000000000201",
        worker_id="fresh-worker",
        process_id=201,
        hostname="test-host",
        queues=["ace-refresh"],
        status=WorkerStatus.ACTIVE,
        last_seen_at=NOW - timedelta(seconds=10),
    )

    readiness = collect_queue_readiness(
        "ace-refresh",
        heartbeat_stale_seconds=60,
        oldest_ready_seconds=300,
        workers_expected=True,
        now=NOW,
    )

    assert readiness.routing_ready is True
    assert readiness.routing_reasons == ()
    assert readiness.has_fresh_worker is True
    assert readiness.ready_depth == 0


def test_queue_readiness_unready_missing_worker() -> None:
    readiness = collect_queue_readiness(
        "ace-refresh",
        heartbeat_stale_seconds=60,
        oldest_ready_seconds=300,
        workers_expected=True,
        now=NOW,
    )

    assert readiness.routing_ready is False
    assert "missing_worker:ace-refresh" in readiness.routing_reasons


def test_queue_readiness_unrelated_queue_does_not_block() -> None:
    """Backlog on another queue does not affect the target queue's readiness."""
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000301",
        activity_key="other-work",
        activity_name="tests.other",
        queue="other-queue",
        status=ActivityStatus.READY,
        available_at=NOW - timedelta(minutes=20),
    )
    AceWorkerHeartbeat.objects.create(
        id="40000000-0000-0000-0000-000000000301",
        worker_id="target-worker",
        process_id=301,
        hostname="test-host",
        queues=["ace-refresh"],
        status=WorkerStatus.ACTIVE,
        last_seen_at=NOW - timedelta(seconds=5),
    )

    readiness = collect_queue_readiness(
        "ace-refresh",
        heartbeat_stale_seconds=60,
        oldest_ready_seconds=300,
        workers_expected=True,
        now=NOW,
    )

    assert readiness.routing_ready is True
    assert readiness.ready_depth == 0


def test_health_reports_failed_groups() -> None:
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000401",
        workflow_name="tests.group-health",
        workflow_version="1",
        status=WorkflowStatus.FAILED,
    )
    ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="test-group",
        status=ActivityGroupStatus.FAILED,
        completed_at=NOW,
    )

    report = collect_ace_health(
        expected_queues=(),
        heartbeat_stale_seconds=60,
        oldest_ready_seconds=300,
        workers_expected=False,
        now=NOW,
    )

    assert report.failed_groups == 1
    assert "failed_groups" in report.unhealthy_reasons


def test_ready_depth_excludes_cancelled_group_members() -> None:
    """Members of a failed group should not count as eligible ready depth."""
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000501",
        workflow_name="tests.group-depth",
        workflow_version="1",
        status=WorkflowStatus.RUNNING,
    )
    group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="test-depth-group",
        status=ActivityGroupStatus.FAILED,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000501",
        workflow_run=run,
        group=group,
        activity_key="member-a",
        activity_name="tests.depth",
        queue="ace-refresh",
        status=ActivityStatus.READY,
        available_at=NOW - timedelta(minutes=1),
    )

    readiness = collect_queue_readiness(
        "ace-refresh",
        heartbeat_stale_seconds=60,
        oldest_ready_seconds=300,
        workers_expected=False,
        now=NOW,
    )

    assert readiness.ready_depth == 0


@patch("ace_django.health.timezone.now", return_value=NOW)
@override_settings(
    # Legacy SUBMISSION_ACE_* names still work as a fallback for ACE_*.
    SUBMISSION_ACE_EXPECTED_QUEUES=("ace-refresh",),
    SUBMISSION_ACE_WORKERS_EXPECTED=True,
    SUBMISSION_ACE_HEARTBEAT_STALE_SECONDS=60,
    SUBMISSION_ACE_OLDEST_READY_SECONDS=300,
)
def test_checkacehealth_compact_queue_readiness(_mock_now) -> None:
    AceWorkerHeartbeat.objects.create(
        id="40000000-0000-0000-0000-000000000601",
        worker_id="compact-worker",
        process_id=601,
        hostname="test-host",
        queues=["ace-refresh"],
        status=WorkerStatus.ACTIVE,
        last_seen_at=NOW,
    )
    output = StringIO()

    call_command(
        "checkacehealth",
        "--queue",
        "ace-refresh",
        "--compact",
        stdout=output,
    )

    result = json.loads(output.getvalue())
    assert result["routing_ready"] is True
    assert result["queue"] == "ace-refresh"
    assert "routing_reasons" in result


# ---------------------------------------------------------------------------
# Group reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_consistent_when_no_groups() -> None:
    report = collect_activity_group_reconciliation(now=NOW)
    assert report.consistent is True
    assert report.inconsistencies == ()


def test_reconciliation_detects_running_group_all_terminal_members() -> None:
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000701",
        workflow_name="tests.recon",
        workflow_version="1",
        status=WorkflowStatus.RUNNING,
    )
    group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="stuck-group",
        status=ActivityGroupStatus.RUNNING,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000701",
        workflow_run=run,
        group=group,
        activity_key="a",
        activity_name="tests.recon",
        status=ActivityStatus.SUCCEEDED,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000702",
        workflow_run=run,
        group=group,
        activity_key="b",
        activity_name="tests.recon",
        status=ActivityStatus.SUCCEEDED,
    )

    report = collect_activity_group_reconciliation(now=NOW)

    assert report.consistent is False
    assert any(i.reason == "running_group_all_members_terminal" for i in report.inconsistencies)


def test_reconciliation_detects_failed_group_no_failed_member() -> None:
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000801",
        workflow_name="tests.recon",
        workflow_version="1",
        status=WorkflowStatus.FAILED,
    )
    group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="bad-fail-group",
        status=ActivityGroupStatus.FAILED,
        failure={"failed_activity_key": "a"},
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000801",
        workflow_run=run,
        group=group,
        activity_key="a",
        activity_name="tests.recon",
        status=ActivityStatus.SUCCEEDED,
    )

    report = collect_activity_group_reconciliation(now=NOW)

    assert report.consistent is False
    assert any(i.reason == "failed_group_no_failed_member" for i in report.inconsistencies)


def test_reconciliation_detects_failed_group_with_claimable_members() -> None:
    """A FAILED group with READY/RETRYING members holds unclaimable zombies."""
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000001001",
        workflow_name="tests.recon",
        workflow_version="1",
        status=WorkflowStatus.FAILED,
    )
    group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="zombie-group",
        status=ActivityGroupStatus.FAILED,
        failure={"failed_activity_key": "a"},
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000001001",
        workflow_run=run,
        group=group,
        activity_key="a",
        activity_name="tests.recon",
        status=ActivityStatus.FAILED,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000001002",
        workflow_run=run,
        group=group,
        activity_key="b",
        activity_name="tests.recon",
        status=ActivityStatus.RETRYING,
    )

    report = collect_activity_group_reconciliation(now=NOW)

    assert report.consistent is False
    assert any(i.reason == "failed_group_has_claimable_members" for i in report.inconsistencies)


def test_reconciliation_detects_running_group_in_terminal_workflow() -> None:
    """A RUNNING group in a terminal workflow can never fan in."""
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000001101",
        workflow_name="tests.recon",
        workflow_version="1",
        status=WorkflowStatus.FAILED,
    )
    group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="orphan-group",
        status=ActivityGroupStatus.RUNNING,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000001101",
        workflow_run=run,
        group=group,
        activity_key="a",
        activity_name="tests.recon",
        status=ActivityStatus.RUNNING,
    )

    report = collect_activity_group_reconciliation(now=NOW)

    assert report.consistent is False
    assert any(i.reason == "running_group_in_terminal_workflow" for i in report.inconsistencies)


def test_reconciliation_terminal_window_excludes_old_terminal_groups() -> None:
    """Terminal groups older than the window are skipped; RUNNING groups are
    always scanned."""
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000001201",
        workflow_name="tests.recon",
        workflow_version="1",
        status=WorkflowStatus.RUNNING,
    )
    old_group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="old-bad-group",
        status=ActivityGroupStatus.FAILED,  # No failure metadata → inconsistent.
    )
    # Age the terminal group beyond the window (updated_at is auto_now).
    ActivityGroupRun.objects.filter(pk=old_group.pk).update(
        updated_at=NOW - timedelta(days=30),
    )
    stuck_group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="stuck-running-group",
        status=ActivityGroupStatus.RUNNING,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000001201",
        workflow_run=run,
        group=stuck_group,
        activity_key="a",
        activity_name="tests.recon",
        status=ActivityStatus.SUCCEEDED,
    )

    windowed = collect_activity_group_reconciliation(now=NOW, terminal_window=timedelta(days=7))
    unwindowed = collect_activity_group_reconciliation(now=NOW)

    # Windowed run skips the old terminal group but still sees the stuck one.
    windowed_reasons = {i.reason for i in windowed.inconsistencies}
    assert "running_group_all_members_terminal" in windowed_reasons
    assert "failed_group_missing_failure" not in windowed_reasons
    # Unwindowed run sees both.
    unwindowed_reasons = {i.reason for i in unwindowed.inconsistencies}
    assert "failed_group_missing_failure" in unwindowed_reasons


def test_checkacereconciliation_passes_when_consistent() -> None:
    output = StringIO()
    call_command("checkacereconciliation", stdout=output)
    result = json.loads(output.getvalue())
    assert result["consistent"] is True


def test_checkacereconciliation_fails_when_inconsistent() -> None:
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000901",
        workflow_name="tests.recon",
        workflow_version="1",
        status=WorkflowStatus.RUNNING,
    )
    group = ActivityGroupRun.objects.create(
        workflow_run=run,
        group_key="cmd-test",
        status=ActivityGroupStatus.RUNNING,
    )
    ActivityRun.objects.create(
        id="20000000-0000-0000-0000-000000000901",
        workflow_run=run,
        group=group,
        activity_key="x",
        activity_name="tests.recon",
        status=ActivityStatus.SUCCEEDED,
    )
    output = StringIO()

    with pytest.raises(CommandError, match="inconsistencies"):
        call_command("checkacereconciliation", "--fail-on-inconsistency", stdout=output)
