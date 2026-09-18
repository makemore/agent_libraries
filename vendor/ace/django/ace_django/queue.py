"""PostgreSQL-coordinated leased activity execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from ace import ActivityGroupCompletionPolicy, WorkflowEventType, WorkflowStatus
from django.db import connection, transaction
from django.db.models import DateTimeField, OuterRef, Q, Subquery
from django.utils import timezone

from ace_django.exceptions import InvalidQueueState, LeaseOwnershipLost, QueueConfigurationError
from ace_django.inbox import enqueue_locked
from ace_django.models import (
    ActivityAttempt,
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    QueueConfig,
    WorkflowRun,
)
from ace_django.retry import retry_policy_from_dict

if TYPE_CHECKING:
    from collections.abc import Callable

    from ace import WorkflowEngine, WorkflowSnapshot
    from ace.json_types import JsonObject, JsonValue


@dataclass(frozen=True)
class ActivityLease:
    activity_run_id: str
    workflow_run_id: str | None
    activity_key: str
    activity_name: str
    activity_version: str
    input: JsonObject
    attempt: int
    worker_id: str
    ownership_token: str
    lease_expires_at: datetime


class DjangoActivityQueue:
    def __init__(
        self,
        engine: WorkflowEngine | None = None,
        token_factory: Callable[[], UUID] = uuid4,
        *,
        use_inbox: bool = False,  # Default False for backward compatibility, set True for 1.0 mode
    ) -> None:
        self._engine = engine
        self._token_factory = token_factory
        self._use_inbox = use_inbox

    def claim(
        self,
        worker_id: str,
        *,
        queues: tuple[str, ...] = ("medium",),
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
        activity_capabilities: frozenset[tuple[str, str]] | None = None,
    ) -> ActivityLease | None:
        if not queues:
            raise QueueConfigurationError("At least one activity queue must be selected.")
        claimed_at = now or timezone.now()

        with transaction.atomic():
            # Step 1: Lock QueueConfig rows in name order (global lock order)
            configs = self._lock_queue_configs(sorted(queues), claimed_at)
            if not configs:
                return None  # All requested queues disabled or missing

            enabled_queues = [c.name for c in configs]

            # Step 2: Find candidate activities
            activity = self._find_candidate(
                enabled_queues, claimed_at, configs, activity_capabilities
            )
            if activity is None:
                return None

            # Step 3: Handle expired lease
            if activity.status == ActivityStatus.RUNNING:
                self._expire_previous_attempt(activity, claimed_at)

            # Step 4: Update rate limit window
            queue_config = next(c for c in configs if c.name == activity.queue)
            self._update_rate_limit_window(queue_config, claimed_at)

            # Step 5: Create new attempt and update activity
            attempt_number = activity.current_attempt + 1
            ownership_token = self._token_factory()
            lease_expires_at = claimed_at + lease_duration

            # Set start-to-close and heartbeat deadlines for the attempt
            start_to_close_deadline = None
            heartbeat_deadline = None
            if activity.start_to_close_seconds is not None:
                start_to_close_deadline = claimed_at + timedelta(
                    seconds=activity.start_to_close_seconds
                )
            if activity.heartbeat_seconds is not None:
                heartbeat_deadline = claimed_at + timedelta(seconds=activity.heartbeat_seconds)

            ActivityAttempt.objects.create(
                activity_run=activity,
                attempt_number=attempt_number,
                worker_id=worker_id,
                ownership_token=ownership_token,
                lease_expires_at=lease_expires_at,
                heartbeat_at=claimed_at,
                started_at=claimed_at,
                start_to_close_deadline=start_to_close_deadline,
                heartbeat_deadline=heartbeat_deadline,
            )
            activity.status = ActivityStatus.RUNNING
            activity.current_attempt = attempt_number
            activity.available_at = None
            if activity.started_at is None:
                activity.started_at = claimed_at
            activity.save(
                update_fields=[
                    "status",
                    "current_attempt",
                    "available_at",
                    "started_at",
                    "updated_at",
                ]
            )
            return _to_lease(activity, worker_id, ownership_token, lease_expires_at)

    def _lock_queue_configs(
        self,
        queue_names: list[str],
        now: datetime,
    ) -> list[QueueConfig]:
        """Lock QueueConfig rows in name order and filter to enabled queues.

        Returns configs that are enabled and pass rate limit checks.
        If no configs exist for the queues, returns synthetic unlimited configs.
        """
        # Lock in name order (already sorted by caller)
        configs = list(
            QueueConfig.objects.filter(name__in=queue_names).order_by("name").select_for_update()
        )

        # Filter to enabled configs
        enabled_configs = [c for c in configs if c.enabled]
        configured_names = {c.name for c in configs}

        # For backward compatibility: queues without a QueueConfig row are allowed
        # with unlimited capacity (no concurrency/rate limits)
        unconfigured = [q for q in queue_names if q not in configured_names]
        for name in unconfigured:
            # Create a synthetic in-memory config for unconfigured queues
            synthetic = QueueConfig(
                name=name,
                enabled=True,
                global_concurrency=None,
                rate_limit_count=None,
                rate_limit_period_seconds=None,
                partition_concurrency=None,
            )
            enabled_configs.append(synthetic)

        return enabled_configs

    def _find_candidate(
        self,
        queue_names: list[str],
        claimed_at: datetime,
        configs: list[QueueConfig],
        activity_capabilities: frozenset[tuple[str, str]] | None = None,
    ) -> ActivityRun | None:
        """Find a claimable activity respecting QoS limits.

        ``activity_capabilities`` restricts claims to advertised
        ``(activity_name, activity_version)`` pairs for version routing;
        ``None`` means no restriction (legacy callers).
        """
        current_lease = ActivityAttempt.objects.filter(
            activity_run_id=OuterRef("pk"),
            attempt_number=OuterRef("current_attempt"),
        ).values("lease_expires_at")[:1]
        ready = Q(status__in=(ActivityStatus.READY, ActivityStatus.RETRYING)) & (
            Q(available_at__isnull=True) | Q(available_at__lte=claimed_at)
        )
        expired = Q(status=ActivityStatus.RUNNING, current_lease_expires__lte=claimed_at)

        # Build excluded queues based on QoS limits
        excluded_queues = self._get_excluded_queues(configs, claimed_at)
        available_queues = [q for q in queue_names if q not in excluded_queues]
        if not available_queues:
            return None

        candidates = (
            ActivityRun.objects.annotate(
                current_lease_expires=Subquery(current_lease, output_field=DateTimeField())
            )
            .filter(ready | expired, queue__in=available_queues)
            .filter(
                Q(workflow_run__isnull=True)
                | Q(
                    workflow_run__cancel_requested_at__isnull=True,
                    workflow_run__status__in=(
                        WorkflowStatus.RUNNING,
                        WorkflowStatus.WAITING,
                    ),
                )
            )
            .filter(Q(group__isnull=True) | Q(group__status=ActivityGroupStatus.RUNNING))
            .order_by("-priority", "available_at", "created_at")
        )
        if activity_capabilities is not None:
            # Version routing: claim only advertised (name, version) pairs.
            capability_filter = Q(pk__in=[])
            for name, version in activity_capabilities:
                capability_filter |= Q(activity_name=name, activity_version=version)
            candidates = candidates.filter(capability_filter)
        if connection.features.has_select_for_update_skip_locked:
            candidates = candidates.select_for_update(skip_locked=True, of=("self",))
        else:
            candidates = candidates.select_for_update(of=("self",))

        # QuerySet iteration eagerly locks the entire result set before Python
        # can return its first item. Limit each lock query to one candidate so
        # concurrent workers can still claim unrelated ready activities.
        rejected = []
        while True:
            activity = candidates.exclude(pk__in=rejected).first()
            if activity is None:
                return None
            queue_config = next((c for c in configs if c.name == activity.queue), None)
            if queue_config and self._activity_passes_constraints(
                activity, queue_config, claimed_at
            ):
                return activity
            # Keep looking after a partition-limited candidate without changing
            # queue/QoS policy or trying the same locked row repeatedly.
            rejected.append(activity.pk)

    def _get_excluded_queues(
        self,
        configs: list[QueueConfig],
        now: datetime,
    ) -> set[str]:
        """Return queues that are at their limits."""
        excluded = set()
        for config in configs:
            # Check global concurrency
            if config.global_concurrency is not None:
                active_count = ActivityAttempt.objects.filter(
                    activity_run__queue=config.name,
                    status=AttemptStatus.RUNNING,
                    lease_expires_at__gt=now,
                ).count()
                if active_count >= config.global_concurrency:
                    excluded.add(config.name)
                    continue

            # Check rate limit
            if config.rate_limit_count is not None and config.rate_limit_period_seconds is not None:
                window_start = config.rate_limit_window_start
                if window_start is not None:
                    window_end = window_start + timedelta(seconds=config.rate_limit_period_seconds)
                    if now < window_end:
                        # Still in current window
                        if config.rate_limit_window_count >= config.rate_limit_count:
                            excluded.add(config.name)
                            continue

        return excluded

    def _activity_passes_constraints(
        self,
        activity: ActivityRun,
        config: QueueConfig,
        now: datetime,
    ) -> bool:
        """Check if activity passes partition constraints."""
        # Check partition concurrency if set
        if config.partition_concurrency is not None and activity.partition_key is not None:
            active_in_partition = (
                ActivityAttempt.objects.filter(
                    activity_run__queue=config.name,
                    activity_run__partition_key=activity.partition_key,
                    status=AttemptStatus.RUNNING,
                    lease_expires_at__gt=now,
                )
                .exclude(activity_run_id=activity.pk)
                .count()
            )
            if active_in_partition >= config.partition_concurrency:
                return False
        return True

    def _expire_previous_attempt(
        self,
        activity: ActivityRun,
        claimed_at: datetime,
    ) -> None:
        """Mark the previous attempt as expired."""
        previous = (
            ActivityAttempt.objects.select_for_update()
            .filter(
                activity_run=activity,
                attempt_number=activity.current_attempt,
            )
            .first()
        )
        if previous is None:
            raise InvalidQueueState(
                f"Running activity {activity.id} has no current attempt {activity.current_attempt}."
            )
        previous.status = AttemptStatus.EXPIRED
        previous.completed_at = claimed_at
        previous.save(update_fields=["status", "completed_at", "updated_at"])

    def _update_rate_limit_window(
        self,
        config: QueueConfig,
        now: datetime,
    ) -> None:
        """Update the fixed-window rate limit for the queue."""
        if config.rate_limit_count is None or config.rate_limit_period_seconds is None:
            return

        window_start = config.rate_limit_window_start
        if window_start is None:
            # Start new window
            config.rate_limit_window_start = now
            config.rate_limit_window_count = 1
            config.save(
                update_fields=["rate_limit_window_start", "rate_limit_window_count", "updated_at"]
            )
        else:
            window_end = window_start + timedelta(seconds=config.rate_limit_period_seconds)
            if now >= window_end:
                # Window expired, start new one
                config.rate_limit_window_start = now
                config.rate_limit_window_count = 1
                config.save(
                    update_fields=[
                        "rate_limit_window_start",
                        "rate_limit_window_count",
                        "updated_at",
                    ]
                )
            else:
                # Increment count in current window
                config.rate_limit_window_count += 1
                config.save(update_fields=["rate_limit_window_count", "updated_at"])

    def renew(
        self,
        lease: ActivityLease,
        *,
        details: JsonObject | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
    ) -> bool:
        heartbeat_at = now or timezone.now()
        updates: dict[str, object] = {
            "heartbeat_at": heartbeat_at,
            "lease_expires_at": heartbeat_at + lease_duration,
            "updated_at": heartbeat_at,
        }
        if details is not None:
            updates["details"] = details
        updated = ActivityAttempt.objects.filter(
            activity_run_id=lease.activity_run_id,
            attempt_number=lease.attempt,
            ownership_token=_token(lease.ownership_token),
            status=AttemptStatus.RUNNING,
            lease_expires_at__gt=heartbeat_at,
            activity_run__current_attempt=lease.attempt,
            activity_run__status=ActivityStatus.RUNNING,
        ).update(**updates)
        return updated == 1

    def complete(
        self,
        lease: ActivityLease,
        result: JsonValue,
        *,
        now: datetime | None = None,
    ) -> WorkflowSnapshot | None:
        completed_at = now or timezone.now()
        with transaction.atomic():
            activity, attempt, workflow, group = _owned_attempt(lease, completed_at)
            if _cancellation_committed(workflow):
                _mark_cancelled(activity, attempt, completed_at, "Workflow cancellation requested.")
                return None
            attempt.status = AttemptStatus.SUCCEEDED
            attempt.completed_at = completed_at
            attempt.save(update_fields=["status", "completed_at", "updated_at"])
            activity.status = ActivityStatus.SUCCEEDED
            activity.result = result
            activity.failure = None
            activity.available_at = None
            activity.completed_at = completed_at
            activity.save(
                update_fields=[
                    "status",
                    "result",
                    "failure",
                    "available_at",
                    "completed_at",
                    "updated_at",
                ]
            )

            # --- Grouped member: fan-in to group ---
            if group is not None:
                if group.status != ActivityGroupStatus.RUNNING:
                    # Group already terminal — record member outcome, emit nothing.
                    return None
                if group.completion_policy == ActivityGroupCompletionPolicy.WAIT_ALL:
                    return self._try_settle_wait_all_group(group, activity, completed_at, workflow)
                return self._try_complete_group(group, activity, completed_at, workflow)

            # --- Standalone activity ---
            if workflow is not None and WorkflowStatus(workflow.status).is_terminal:
                return None

            # 1.0 inbox mode: enqueue event for dispatcher
            if self._use_inbox and workflow is not None:
                enqueue_locked(
                    workflow,
                    source_type="activity",
                    source_key=activity.activity_key,
                    event_type=WorkflowEventType.ACTIVITY_COMPLETED,
                    payload={
                        "activity_key": activity.activity_key,
                        "activity_run_id": str(activity.id),
                        "result": result,
                    },
                    occurred_at=completed_at,
                )
                return None  # No immediate snapshot in inbox mode

            # Legacy mode: direct engine call
            engine = self._engine_for(activity)
            if engine is None:
                return None
            return engine.handle_event(
                str(activity.workflow_run_id),
                WorkflowEventType.ACTIVITY_COMPLETED,
                payload={
                    "activity_key": activity.activity_key,
                    "activity_run_id": str(activity.id),
                    "result": result,
                },
            )

    def fail(
        self,
        lease: ActivityLease,
        *,
        error_type: str,
        message: str,
        details: JsonObject | None = None,
        now: datetime | None = None,
    ) -> WorkflowSnapshot | None:
        failed_at = now or timezone.now()
        with transaction.atomic():
            activity, attempt, workflow, group = _owned_attempt(lease, failed_at)
            if _cancellation_committed(workflow):
                _mark_cancelled(activity, attempt, failed_at, "Workflow cancellation requested.")
                return None
            policy = retry_policy_from_dict(activity.retry_policy)
            attempt.status = AttemptStatus.FAILED
            attempt.completed_at = failed_at
            attempt.error_type = error_type
            attempt.error_message = message
            attempt.details = details or {}
            attempt.save(
                update_fields=[
                    "status",
                    "completed_at",
                    "error_type",
                    "error_message",
                    "details",
                    "updated_at",
                ]
            )
            failure = {"error_type": error_type, "message": message, "details": details or {}}
            activity.failure = failure
            workflow_is_draining = (
                workflow is not None and workflow.status == WorkflowStatus.CANCELLING
            )
            # A member of an already-terminal group must not retry: the group can
            # never claim it again, so RETRYING would be an unclaimable zombie row.
            group_is_terminal = group is not None and group.status != ActivityGroupStatus.RUNNING
            if (
                attempt.attempt_number < policy.max_attempts
                and not workflow_is_draining
                and not group_is_terminal
            ):
                activity.status = ActivityStatus.RETRYING
                activity.available_at = failed_at + timedelta(
                    seconds=policy.delay_after(attempt.attempt_number)
                )
                activity.save(update_fields=["status", "failure", "available_at", "updated_at"])
                return None

            activity.status = ActivityStatus.FAILED
            activity.completed_at = failed_at
            activity.save(update_fields=["status", "failure", "completed_at", "updated_at"])

            # --- Grouped member: fail-fast (ALL_SUCCESS) or settle-all (WAIT_ALL) ---
            if group is not None:
                if group.status != ActivityGroupStatus.RUNNING:
                    return None
                if group.completion_policy == ActivityGroupCompletionPolicy.WAIT_ALL:
                    return self._try_settle_wait_all_group(group, activity, failed_at, workflow)
                return self._fail_group(group, activity, failure, failed_at, workflow)

            # --- Standalone activity ---
            if workflow is not None and WorkflowStatus(workflow.status).is_terminal:
                return None

            # 1.0 inbox mode: enqueue event for dispatcher
            if self._use_inbox and workflow is not None:
                enqueue_locked(
                    workflow,
                    source_type="activity",
                    source_key=activity.activity_key,
                    event_type=WorkflowEventType.ACTIVITY_FAILED,
                    payload={
                        "activity_key": activity.activity_key,
                        "activity_run_id": str(activity.id),
                        "failure": failure,
                    },
                    occurred_at=failed_at,
                )
                return None  # No immediate snapshot in inbox mode

            # Legacy mode: direct engine call
            engine = self._engine_for(activity)
            if engine is None:
                return None
            return engine.handle_event(
                str(activity.workflow_run_id),
                WorkflowEventType.ACTIVITY_FAILED,
                payload={
                    "activity_key": activity.activity_key,
                    "activity_run_id": str(activity.id),
                    "failure": failure,
                },
            )

    def cancel(
        self,
        lease: ActivityLease,
        *,
        message: str,
        now: datetime | None = None,
    ) -> None:
        cancelled_at = now or timezone.now()
        with transaction.atomic():
            activity, attempt, workflow, group = _owned_attempt(lease, cancelled_at)
            _mark_cancelled(activity, attempt, cancelled_at, message)
            # Grouped members do not emit individual ACTIVITY_CANCELLED events.
            if group is not None:
                return
            if workflow is None or workflow.status != WorkflowStatus.CANCELLING:
                return
            if self._use_inbox:
                enqueue_locked(
                    workflow,
                    source_type="activity",
                    source_key=activity.activity_key,
                    event_type=WorkflowEventType.ACTIVITY_CANCELLED,
                    payload={
                        "activity_key": activity.activity_key,
                        "activity_run_id": str(activity.id),
                        "message": message,
                    },
                    occurred_at=cancelled_at,
                )
                return
            engine = self._engine_for(activity)
            if engine is not None:
                engine.handle_event(
                    str(activity.workflow_run_id),
                    WorkflowEventType.ACTIVITY_CANCELLED,
                    payload={
                        "activity_key": activity.activity_key,
                        "activity_run_id": str(activity.id),
                        "message": message,
                    },
                )

    def cancellation_requested(self, lease: ActivityLease) -> bool:
        return ActivityRun.objects.filter(
            pk=lease.activity_run_id,
            workflow_run__cancel_requested_at__isnull=False,
        ).exists()

    def _try_complete_group(
        self,
        group: ActivityGroupRun,
        completed_activity: ActivityRun,
        completed_at: datetime,
        workflow: WorkflowRun | None,
    ) -> WorkflowSnapshot | None:
        """If all members are SUCCEEDED, mark group SUCCEEDED and emit one event."""
        members = list(group.members.order_by("activity_key"))
        if any(m.status != ActivityStatus.SUCCEEDED for m in members):
            # Not all done yet — emit nothing.
            return None
        # All members succeeded — build sorted result map.
        results = {m.activity_key: m.result for m in members}
        group.status = ActivityGroupStatus.SUCCEEDED
        group.result = results
        group.completed_at = completed_at
        group.save(update_fields=["status", "result", "completed_at", "updated_at"])
        if workflow is not None and WorkflowStatus(workflow.status).is_terminal:
            # Workflow reached a terminal state while members were in flight —
            # record the group outcome, emit nothing.
            return None

        # 1.0 inbox mode: enqueue event for dispatcher
        if self._use_inbox and workflow is not None:
            enqueue_locked(
                workflow,
                source_type="group",
                source_key=group.group_key,
                event_type=WorkflowEventType.ACTIVITY_GROUP_COMPLETED,
                payload={
                    "group_key": group.group_key,
                    "group_run_id": str(group.pk),
                    "results": results,
                },
                occurred_at=completed_at,
            )
            return None  # No immediate snapshot in inbox mode

        # Legacy mode: direct engine call
        engine = self._engine_for(completed_activity)
        if engine is None:
            return None
        return engine.handle_event(
            str(completed_activity.workflow_run_id),
            WorkflowEventType.ACTIVITY_GROUP_COMPLETED,
            payload={
                "group_key": group.group_key,
                "group_run_id": str(group.pk),
                "results": results,
            },
        )

    def _try_settle_wait_all_group(
        self,
        group: ActivityGroupRun,
        settling_activity: ActivityRun,
        settled_at: datetime,
        workflow: WorkflowRun | None,
    ) -> WorkflowSnapshot | None:
        """WAIT_ALL: settle once every member reaches a terminal per-member
        status (SUCCEEDED/FAILED/CANCELLED). Siblings are never cancelled on
        a member failure. Always emits a single ACTIVITY_GROUP_COMPLETED
        event carrying both `results` and `failures` — the workflow's own
        transition logic decides whether partial failure is acceptable.
        """
        members = list(group.members.order_by("activity_key"))
        terminal = (ActivityStatus.SUCCEEDED, ActivityStatus.FAILED, ActivityStatus.CANCELLED)
        if any(m.status not in terminal for m in members):
            # Still waiting on siblings — emit nothing.
            return None

        results = {
            m.activity_key: m.result for m in members if m.status == ActivityStatus.SUCCEEDED
        }
        failures = {
            m.activity_key: m.failure for m in members if m.status != ActivityStatus.SUCCEEDED
        }

        group.status = ActivityGroupStatus.FAILED if failures else ActivityGroupStatus.SUCCEEDED
        group.result = results
        group.failure = {"failures": failures} if failures else None
        group.completed_at = settled_at
        group.save(update_fields=["status", "result", "failure", "completed_at", "updated_at"])

        if workflow is not None and WorkflowStatus(workflow.status).is_terminal:
            # Workflow reached a terminal state while members were in flight —
            # record the group outcome, emit nothing.
            return None

        payload = {
            "group_key": group.group_key,
            "group_run_id": str(group.pk),
            "results": results,
            "failures": failures,
        }

        # 1.0 inbox mode: enqueue event for dispatcher
        if self._use_inbox and workflow is not None:
            enqueue_locked(
                workflow,
                source_type="group",
                source_key=group.group_key,
                event_type=WorkflowEventType.ACTIVITY_GROUP_COMPLETED,
                payload=payload,
                occurred_at=settled_at,
            )
            return None  # No immediate snapshot in inbox mode

        # Legacy mode: direct engine call
        engine = self._engine_for(settling_activity)
        if engine is None:
            return None
        return engine.handle_event(
            str(settling_activity.workflow_run_id),
            WorkflowEventType.ACTIVITY_GROUP_COMPLETED,
            payload=payload,
        )

    def _fail_group(
        self,
        group: ActivityGroupRun,
        failed_activity: ActivityRun,
        failure: JsonObject,
        failed_at: datetime,
        workflow: WorkflowRun | None,
    ) -> WorkflowSnapshot | None:
        """Fail-fast: mark group FAILED, cancel unclaimed siblings, emit one event."""
        # Collect results from already-succeeded members.
        succeeded_members = list(
            group.members.filter(status=ActivityStatus.SUCCEEDED).order_by("activity_key")
        )
        results = {m.activity_key: m.result for m in succeeded_members}

        group.status = ActivityGroupStatus.FAILED
        group.result = results
        group.failure = {
            "failed_activity_key": failed_activity.activity_key,
            "failed_activity_run_id": str(failed_activity.pk),
            "failure": failure,
        }
        group.completed_at = failed_at
        group.save(update_fields=["status", "result", "failure", "completed_at", "updated_at"])

        # Cancel only READY/RETRYING siblings.
        group.members.filter(
            status__in=(ActivityStatus.READY, ActivityStatus.RETRYING),
        ).update(
            status=ActivityStatus.CANCELLED,
            completed_at=failed_at,
            available_at=None,
            updated_at=failed_at,
        )

        if workflow is not None and WorkflowStatus(workflow.status).is_terminal:
            # Workflow reached a terminal state while members were in flight —
            # record the group outcome, emit nothing.
            return None

        # 1.0 inbox mode: enqueue event for dispatcher
        if self._use_inbox and workflow is not None:
            enqueue_locked(
                workflow,
                source_type="group",
                source_key=group.group_key,
                event_type=WorkflowEventType.ACTIVITY_GROUP_FAILED,
                payload={
                    "group_key": group.group_key,
                    "group_run_id": str(group.pk),
                    "results": results,
                    "failed_activity_key": failed_activity.activity_key,
                    "failed_activity_run_id": str(failed_activity.pk),
                    "failure": failure,
                },
                occurred_at=failed_at,
            )
            return None  # No immediate snapshot in inbox mode

        # Legacy mode: direct engine call
        engine = self._engine_for(failed_activity)
        if engine is None:
            return None
        return engine.handle_event(
            str(failed_activity.workflow_run_id),
            WorkflowEventType.ACTIVITY_GROUP_FAILED,
            payload={
                "group_key": group.group_key,
                "group_run_id": str(group.pk),
                "results": results,
                "failed_activity_key": failed_activity.activity_key,
                "failed_activity_run_id": str(failed_activity.pk),
                "failure": failure,
            },
        )

    def _engine_for(self, activity: ActivityRun) -> WorkflowEngine | None:
        if activity.workflow_run_id is None:
            return None
        if self._engine is None:
            raise QueueConfigurationError(
                f"Activity {activity.id} belongs to workflow {activity.workflow_run_id}, "
                "but the queue has no WorkflowEngine."
            )
        return self._engine


def _owned_attempt(
    lease: ActivityLease,
    now: datetime,
) -> tuple[ActivityRun, ActivityAttempt, WorkflowRun | None, ActivityGroupRun | None]:
    """Lock in global order: WorkflowRun → ActivityGroupRun → ActivityRun → ActivityAttempt."""
    # Read IDs first (no lock).
    ids = (
        ActivityRun.objects.filter(pk=lease.activity_run_id)
        .values_list("workflow_run_id", "group_id")
        .get()
    )
    workflow_run_id, group_id = ids

    # Lock in global order.
    workflow = (
        WorkflowRun.objects.select_for_update().get(pk=workflow_run_id)
        if workflow_run_id is not None
        else None
    )
    group = (
        ActivityGroupRun.objects.select_for_update().get(pk=group_id)
        if group_id is not None
        else None
    )
    activity = ActivityRun.objects.select_for_update().get(pk=lease.activity_run_id)
    attempt = (
        ActivityAttempt.objects.select_for_update()
        .filter(
            activity_run=activity,
            attempt_number=lease.attempt,
            ownership_token=_token(lease.ownership_token),
            status=AttemptStatus.RUNNING,
        )
        .first()
    )
    if (
        attempt is None
        or activity.current_attempt != lease.attempt
        or activity.status != ActivityStatus.RUNNING
        or attempt.lease_expires_at <= now
    ):
        raise LeaseOwnershipLost(
            f"Activity {lease.activity_run_id!r} attempt {lease.attempt} no longer owns the lease."
        )
    return activity, attempt, workflow, group


def _cancellation_committed(workflow: WorkflowRun | None) -> bool:
    return workflow is not None and workflow.status == WorkflowStatus.CANCELLED


def _mark_cancelled(
    activity: ActivityRun,
    attempt: ActivityAttempt,
    cancelled_at: datetime,
    message: str,
) -> None:
    attempt.status = AttemptStatus.CANCELLED
    attempt.completed_at = cancelled_at
    attempt.error_type = "ActivityCancelled"
    attempt.error_message = message
    attempt.save(
        update_fields=[
            "status",
            "completed_at",
            "error_type",
            "error_message",
            "updated_at",
        ]
    )
    activity.status = ActivityStatus.CANCELLED
    activity.completed_at = cancelled_at
    activity.failure = {"error_type": "ActivityCancelled", "message": message}
    activity.save(update_fields=["status", "completed_at", "failure", "updated_at"])


def _to_lease(
    activity: ActivityRun,
    worker_id: str,
    ownership_token: UUID,
    lease_expires_at: datetime,
) -> ActivityLease:
    return ActivityLease(
        activity_run_id=str(activity.id),
        workflow_run_id=(str(activity.workflow_run_id) if activity.workflow_run_id else None),
        activity_key=activity.activity_key,
        activity_name=activity.activity_name,
        activity_version=activity.activity_version,
        input=activity.input,
        attempt=activity.current_attempt,
        worker_id=worker_id,
        ownership_token=str(ownership_token),
        lease_expires_at=lease_expires_at,
    )


def _token(value: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise LeaseOwnershipLost(f"Lease ownership token {value!r} is invalid.") from exc
