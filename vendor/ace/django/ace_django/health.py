"""Read-only operational health evidence for ACE queues and workers.

This module provides two distinct health concepts:
1. Readiness: Can the system accept new work? (DB, schema, services, routing)
2. Failure metrics: Historical counts of failures (for alerting, not readiness)

Historical failures do NOT fail readiness. A system with failed workflows
in the past is still ready to accept new work if services are running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ace import WorkflowStatus
from django.db import connection
from django.db.models import F, Min, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityAttempt,
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    InboxStatus,
    QueueConfig,
    TimerStatus,
    WorkerStatus,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTimer,
    WorkflowTransitionFailure,
)

READY_STATUSES = (ActivityStatus.READY, ActivityStatus.RETRYING)


def _eligible_ready(queue: str, now: datetime) -> Q:
    """Filter for eligible READY/RETRYING work on a queue — mirrors claim logic."""
    return (
        Q(queue=queue)
        & Q(status__in=READY_STATUSES)
        & (Q(available_at__isnull=True) | Q(available_at__lte=now))
        & (
            Q(workflow_run__isnull=True)
            | Q(
                workflow_run__cancel_requested_at__isnull=True,
                workflow_run__status__in=(WorkflowStatus.RUNNING, WorkflowStatus.WAITING),
            )
        )
        & (Q(group__isnull=True) | Q(group__status=ActivityGroupStatus.RUNNING))
    )


@dataclass(frozen=True)
class QueueHealth:
    queue: str
    depth: int
    ready_depth: int
    oldest_ready_at: datetime | None
    oldest_ready_age_seconds: float | None
    has_fresh_worker: bool
    expired_active_leases: int
    routing_ready: bool
    routing_reasons: tuple[str, ...]


@dataclass(frozen=True)
class AceHealthReport:
    checked_at: datetime
    workers_expected: bool
    queues: tuple[QueueHealth, ...]
    expired_leases: int
    failed_workflows: int
    failed_activities: int
    failed_groups: int
    stale_heartbeats: int
    unhealthy_reasons: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.unhealthy_reasons


def _queue_health(
    queue: str,
    *,
    now: datetime,
    heartbeat_cutoff: datetime,
    oldest_ready_seconds: int,
    workers_expected: bool,
    is_expected_queue: bool,
) -> QueueHealth:
    # Total depth: all READY/RETRYING on this queue regardless of eligibility.
    waiting = ActivityRun.objects.filter(queue=queue, status__in=READY_STATUSES)
    depth = waiting.count()

    # Eligible ready depth: mirrors claim filters.
    eligible = ActivityRun.objects.filter(_eligible_ready(queue, now))
    ready_depth = eligible.count()
    oldest_ready_at = eligible.aggregate(
        oldest=Min(Coalesce("available_at", "created_at")),
    )["oldest"]
    oldest_age = (now - oldest_ready_at).total_seconds() if oldest_ready_at else None

    # Expired active leases: RUNNING activities on this queue whose current
    # attempt lease has expired.
    expired_active_leases = ActivityAttempt.objects.filter(
        activity_run__queue=queue,
        activity_run__status=ActivityStatus.RUNNING,
        attempt_number=F("activity_run__current_attempt"),
        status=AttemptStatus.RUNNING,
        lease_expires_at__lt=now,
    ).count()

    # Fresh worker check.
    fresh_workers = AceWorkerHeartbeat.objects.filter(
        status=WorkerStatus.ACTIVE,
        last_seen_at__gte=heartbeat_cutoff,
    ).only("queues")
    has_fresh_worker = any(queue in heartbeat.queues for heartbeat in fresh_workers)

    # Routing readiness (queue-local).
    routing_reasons: list[str] = []
    if workers_expected and is_expected_queue and not has_fresh_worker:
        routing_reasons.append(f"missing_worker:{queue}")
    if oldest_age is not None and oldest_age > oldest_ready_seconds:
        routing_reasons.append(f"oldest_ready:{queue}")
    if expired_active_leases > 0:
        routing_reasons.append(f"expired_active_lease:{queue}")
    routing_ready = not routing_reasons

    return QueueHealth(
        queue=queue,
        depth=depth,
        ready_depth=ready_depth,
        oldest_ready_at=oldest_ready_at,
        oldest_ready_age_seconds=oldest_age,
        has_fresh_worker=has_fresh_worker,
        expired_active_leases=expired_active_leases,
        routing_ready=routing_ready,
        routing_reasons=tuple(routing_reasons),
    )


@dataclass(frozen=True)
class QueueReadiness:
    """Queue-specific routing readiness for a single queue."""

    queue: str
    routing_ready: bool
    routing_reasons: tuple[str, ...]
    ready_depth: int
    oldest_ready_age_seconds: float | None
    has_fresh_worker: bool
    expired_active_leases: int


def collect_queue_readiness(
    queue: str,
    *,
    heartbeat_stale_seconds: int,
    oldest_ready_seconds: int,
    workers_expected: bool,
    now: datetime | None = None,
) -> QueueReadiness:
    """Collect routing readiness for a single queue."""
    checked_at = now or timezone.now()
    heartbeat_cutoff = checked_at - timedelta(seconds=heartbeat_stale_seconds)
    qh = _queue_health(
        queue,
        now=checked_at,
        heartbeat_cutoff=heartbeat_cutoff,
        oldest_ready_seconds=oldest_ready_seconds,
        workers_expected=workers_expected,
        is_expected_queue=True,
    )
    return QueueReadiness(
        queue=qh.queue,
        routing_ready=qh.routing_ready,
        routing_reasons=qh.routing_reasons,
        ready_depth=qh.ready_depth,
        oldest_ready_age_seconds=qh.oldest_ready_age_seconds,
        has_fresh_worker=qh.has_fresh_worker,
        expired_active_leases=qh.expired_active_leases,
    )


def collect_ace_health(
    *,
    expected_queues: tuple[str, ...],
    heartbeat_stale_seconds: int,
    oldest_ready_seconds: int,
    workers_expected: bool,
    now: datetime | None = None,
) -> AceHealthReport:
    checked_at = now or timezone.now()
    heartbeat_cutoff = checked_at - timedelta(seconds=heartbeat_stale_seconds)
    observed = ActivityRun.objects.values_list("queue", flat=True).distinct()
    queue_names = tuple(sorted(set(expected_queues).union(observed)))
    queues = tuple(
        _queue_health(
            queue,
            now=checked_at,
            heartbeat_cutoff=heartbeat_cutoff,
            oldest_ready_seconds=oldest_ready_seconds,
            workers_expected=workers_expected,
            is_expected_queue=queue in expected_queues,
        )
        for queue in queue_names
    )
    expired_leases = ActivityAttempt.objects.filter(
        status=AttemptStatus.RUNNING,
        lease_expires_at__lt=checked_at,
    ).count()
    failed_workflows = WorkflowRun.objects.filter(status=WorkflowStatus.FAILED).count()
    failed_activities = ActivityRun.objects.filter(status=ActivityStatus.FAILED).count()
    failed_groups = ActivityGroupRun.objects.filter(
        status=ActivityGroupStatus.FAILED,
    ).count()
    stale_heartbeats = AceWorkerHeartbeat.objects.filter(
        status=WorkerStatus.ACTIVE,
        last_seen_at__lt=heartbeat_cutoff,
    ).count()
    # Operational unhealthy reasons (historical failures included).
    reasons: list[str] = []
    if expired_leases:
        reasons.append("expired_leases")
    if failed_workflows:
        reasons.append("failed_workflows")
    if failed_activities:
        reasons.append("failed_activities")
    if failed_groups:
        reasons.append("failed_groups")
    for queue in queues:
        if queue.oldest_ready_age_seconds is not None and (
            queue.oldest_ready_age_seconds > oldest_ready_seconds
        ):
            reasons.append(f"oldest_ready:{queue.queue}")
        if workers_expected and queue.queue in expected_queues and not queue.has_fresh_worker:
            reasons.append(f"missing_worker:{queue.queue}")
    return AceHealthReport(
        checked_at=checked_at,
        workers_expected=workers_expected,
        queues=queues,
        expired_leases=expired_leases,
        failed_workflows=failed_workflows,
        failed_activities=failed_activities,
        failed_groups=failed_groups,
        stale_heartbeats=stale_heartbeats,
        unhealthy_reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class GroupInconsistency:
    group_id: str
    group_key: str
    reason: str


@dataclass(frozen=True)
class ActivityGroupReconciliationReport:
    checked_at: datetime
    inconsistencies: tuple[GroupInconsistency, ...]

    @property
    def consistent(self) -> bool:
        return not self.inconsistencies


def collect_activity_group_reconciliation(
    *,
    now: datetime | None = None,
    terminal_window: timedelta | None = None,
) -> ActivityGroupReconciliationReport:
    """Detect group state inconsistencies that indicate data corruption or bugs.

    ``terminal_window`` bounds how far back terminal (SUCCEEDED/FAILED/CANCELLED)
    groups are scanned, keyed on ``updated_at``. RUNNING groups are always
    scanned in full — they are the live set and stay small.
    """
    checked_at = now or timezone.now()
    issues: list[GroupInconsistency] = []
    terminal_filter = Q()
    if terminal_window is not None:
        terminal_filter = Q(updated_at__gte=checked_at - terminal_window)

    # 1. SUCCEEDED groups with non-succeeded members or result mismatch.
    for group in ActivityGroupRun.objects.filter(
        terminal_filter, status=ActivityGroupStatus.SUCCEEDED
    ):
        members = list(group.members.order_by("activity_key"))
        non_succeeded = [m for m in members if m.status != ActivityStatus.SUCCEEDED]
        if non_succeeded:
            issues.append(
                GroupInconsistency(
                    group_id=str(group.pk),
                    group_key=group.group_key,
                    reason=f"succeeded_group_has_non_succeeded_members:{','.join(m.activity_key for m in non_succeeded)}",
                )
            )
        expected_result = {m.activity_key: m.result for m in members}
        if group.result != expected_result:
            issues.append(
                GroupInconsistency(
                    group_id=str(group.pk),
                    group_key=group.group_key,
                    reason="succeeded_group_result_mismatch",
                )
            )

    # 2. FAILED groups without failure metadata or without a failed member.
    for group in ActivityGroupRun.objects.filter(
        terminal_filter, status=ActivityGroupStatus.FAILED
    ):
        if not group.failure:
            issues.append(
                GroupInconsistency(
                    group_id=str(group.pk),
                    group_key=group.group_key,
                    reason="failed_group_missing_failure",
                )
            )
        has_failed_member = group.members.filter(status=ActivityStatus.FAILED).exists()
        if not has_failed_member:
            issues.append(
                GroupInconsistency(
                    group_id=str(group.pk),
                    group_key=group.group_key,
                    reason="failed_group_no_failed_member",
                )
            )

    # 3. RUNNING groups with all members terminal.
    for group in ActivityGroupRun.objects.filter(status=ActivityGroupStatus.RUNNING):
        non_terminal = group.members.exclude(
            status__in=(ActivityStatus.SUCCEEDED, ActivityStatus.FAILED, ActivityStatus.CANCELLED),
        ).exists()
        if not non_terminal:
            issues.append(
                GroupInconsistency(
                    group_id=str(group.pk),
                    group_key=group.group_key,
                    reason="running_group_all_members_terminal",
                )
            )

    # 4. Terminal (FAILED/CANCELLED) groups with claimable members. Such
    # members are unclaimable zombies: claim excludes non-RUNNING groups.
    for group in ActivityGroupRun.objects.filter(
        terminal_filter,
        status__in=(ActivityGroupStatus.FAILED, ActivityGroupStatus.CANCELLED),
    ):
        claimable = group.members.filter(status__in=READY_STATUSES).exists()
        if claimable:
            issues.append(
                GroupInconsistency(
                    group_id=str(group.pk),
                    group_key=group.group_key,
                    reason=f"{group.status.lower()}_group_has_claimable_members",
                )
            )

    # 5. RUNNING groups whose workflow is already terminal — the group can
    # never fan in because terminal workflows reject further events.
    terminal_workflow_statuses = tuple(
        status.value for status in WorkflowStatus if status.is_terminal
    )
    for group in ActivityGroupRun.objects.filter(
        status=ActivityGroupStatus.RUNNING,
        workflow_run__status__in=terminal_workflow_statuses,
    ):
        issues.append(
            GroupInconsistency(
                group_id=str(group.pk),
                group_key=group.group_key,
                reason="running_group_in_terminal_workflow",
            )
        )

    return ActivityGroupReconciliationReport(
        checked_at=checked_at,
        inconsistencies=tuple(issues),
    )


# ============================================================================
# Readiness vs Failure Metrics (1.0 API)
# ============================================================================


@dataclass(frozen=True)
class AceReadinessReport:
    """System readiness for accepting new work.

    Historical failures do NOT affect readiness. A system with past failures
    is still ready if services are running and routing is configured.
    """

    checked_at: datetime
    ready: bool
    reasons: tuple[str, ...]
    # Component checks
    database_connected: bool
    dispatcher_fresh: bool
    timer_service_fresh: bool
    deadline_service_fresh: bool
    # Queue routing
    queue_configs: int
    queues_with_workers: int
    # Inbox/timer age (for staleness detection)
    oldest_pending_inbox_age_seconds: float | None
    oldest_scheduled_timer_age_seconds: float | None


@dataclass(frozen=True)
class AceFailureMetrics:
    """Historical failure counts for alerting and monitoring.

    These are metrics, not readiness checks. A system can be ready while
    having historical failures.
    """

    checked_at: datetime
    # Run-level failures
    blocked_workflows: int
    failed_workflows: int
    cancelled_workflows: int
    # Activity failures
    failed_activities: int
    failed_groups: int
    # Inbox/timer DLQ
    inbox_dead_letter: int
    timer_dead_letter: int
    # Transition failures (recent)
    transition_failures_24h: int
    # Timeout counts
    schedule_to_close_timeouts: int
    start_to_close_timeouts: int
    heartbeat_timeouts: int


def collect_ace_readiness(
    *,
    heartbeat_stale_seconds: int = 60,
    now: datetime | None = None,
) -> AceReadinessReport:
    """Collect system readiness for accepting new work.

    Readiness checks:
    - Database connection
    - Fresh dispatcher heartbeat
    - Fresh timer service heartbeat
    - Fresh deadline service heartbeat
    - Queue configs exist
    - At least one queue has a fresh worker

    Historical failures do NOT fail readiness.
    """
    checked_at = now or timezone.now()
    heartbeat_cutoff = checked_at - timedelta(seconds=heartbeat_stale_seconds)
    reasons: list[str] = []

    # Database check
    try:
        connection.ensure_connection()
        database_connected = True
    except Exception:
        database_connected = False
        reasons.append("database_not_connected")

    # Service heartbeats by role
    dispatcher_fresh = AceWorkerHeartbeat.objects.filter(
        role="DISPATCHER",
        status=WorkerStatus.ACTIVE,
        last_seen_at__gte=heartbeat_cutoff,
    ).exists()
    if not dispatcher_fresh:
        reasons.append("dispatcher_not_fresh")

    timer_service_fresh = AceWorkerHeartbeat.objects.filter(
        role="TIMER",
        status=WorkerStatus.ACTIVE,
        last_seen_at__gte=heartbeat_cutoff,
    ).exists()
    if not timer_service_fresh:
        reasons.append("timer_service_not_fresh")

    deadline_service_fresh = AceWorkerHeartbeat.objects.filter(
        role="DEADLINE",
        status=WorkerStatus.ACTIVE,
        last_seen_at__gte=heartbeat_cutoff,
    ).exists()
    if not deadline_service_fresh:
        reasons.append("deadline_service_not_fresh")

    # Queue configs
    queue_configs = QueueConfig.objects.filter(enabled=True).count()
    if queue_configs == 0:
        reasons.append("no_enabled_queues")

    # Workers per queue
    fresh_workers = AceWorkerHeartbeat.objects.filter(
        role="ACTIVITY",
        status=WorkerStatus.ACTIVE,
        last_seen_at__gte=heartbeat_cutoff,
    )
    covered_queues = set()
    for worker in fresh_workers:
        if worker.queues:
            covered_queues.update(worker.queues)
    queues_with_workers = len(covered_queues)

    # Oldest pending inbox
    oldest_pending = WorkflowInboxEvent.objects.filter(
        status__in=(InboxStatus.PENDING, InboxStatus.RETRYING),
    ).aggregate(oldest=Min("created_at"))["oldest"]
    oldest_inbox_age = (checked_at - oldest_pending).total_seconds() if oldest_pending else None

    # Oldest scheduled timer
    oldest_timer = WorkflowTimer.objects.filter(
        status=TimerStatus.SCHEDULED,
        fire_at__lte=checked_at,
    ).aggregate(oldest=Min("fire_at"))["oldest"]
    oldest_timer_age = (checked_at - oldest_timer).total_seconds() if oldest_timer else None

    return AceReadinessReport(
        checked_at=checked_at,
        ready=not reasons,
        reasons=tuple(reasons),
        database_connected=database_connected,
        dispatcher_fresh=dispatcher_fresh,
        timer_service_fresh=timer_service_fresh,
        deadline_service_fresh=deadline_service_fresh,
        queue_configs=queue_configs,
        queues_with_workers=queues_with_workers,
        oldest_pending_inbox_age_seconds=oldest_inbox_age,
        oldest_scheduled_timer_age_seconds=oldest_timer_age,
    )


def collect_ace_failure_metrics(
    *,
    now: datetime | None = None,
) -> AceFailureMetrics:
    """Collect failure metrics for alerting and monitoring.

    These are counts, not readiness checks. Use for dashboards and alerts.
    """
    checked_at = now or timezone.now()
    day_ago = checked_at - timedelta(hours=24)

    # Workflow counts
    blocked = WorkflowRun.objects.filter(status=WorkflowStatus.BLOCKED).count()
    failed = WorkflowRun.objects.filter(status=WorkflowStatus.FAILED).count()
    cancelled = WorkflowRun.objects.filter(status=WorkflowStatus.CANCELLED).count()

    # Activity counts
    failed_activities = ActivityRun.objects.filter(status=ActivityStatus.FAILED).count()
    failed_groups = ActivityGroupRun.objects.filter(status=ActivityGroupStatus.FAILED).count()

    # DLQ counts
    inbox_dlq = WorkflowInboxEvent.objects.filter(status=InboxStatus.DEAD_LETTER).count()
    timer_dlq = WorkflowTimer.objects.filter(status=TimerStatus.DEAD_LETTER).count()

    # Recent transition failures
    transition_failures = WorkflowTransitionFailure.objects.filter(
        created_at__gte=day_ago,
    ).count()

    # Timeout counts (from activity attempts)
    schedule_to_close = ActivityAttempt.objects.filter(
        timeout_cause="schedule_to_close",
    ).count()
    start_to_close = ActivityAttempt.objects.filter(
        timeout_cause="start_to_close",
    ).count()
    heartbeat = ActivityAttempt.objects.filter(
        timeout_cause="heartbeat",
    ).count()

    return AceFailureMetrics(
        checked_at=checked_at,
        blocked_workflows=blocked,
        failed_workflows=failed,
        cancelled_workflows=cancelled,
        failed_activities=failed_activities,
        failed_groups=failed_groups,
        inbox_dead_letter=inbox_dlq,
        timer_dead_letter=timer_dlq,
        transition_failures_24h=transition_failures,
        schedule_to_close_timeouts=schedule_to_close,
        start_to_close_timeouts=start_to_close,
        heartbeat_timeouts=heartbeat,
    )
