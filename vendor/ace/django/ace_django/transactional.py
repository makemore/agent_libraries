"""Transactional activity execution for same-database atomicity.

DjangoTransactionalActivityExecutor provides exactly-once completion semantics
for activities that only write to the same database as ACE:

1. Opens transaction.atomic(using=ACE_DATABASE_ALIAS)
2. Acquires all ACE locks (workflow -> group -> activity -> attempt)
3. Invokes the synchronous callable
4. Commits callable writes + activity completion + inbox event together

On exception or lease timeout, all writes roll back before failure handling.

IMPORTANT LIMITATIONS:
- Only works for writes to the ACE database
- No heartbeat thread (synchronous execution only)
- No network calls or external side effects
- Activity must complete within lease duration
- Standard/external activities remain at-least-once
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ace import WorkflowEventType, WorkflowStatus
from django.db import transaction
from django.utils import timezone

from ace_django.inbox import enqueue_locked
from ace_django.models import (
    ActivityAttempt,
    ActivityExecutionMode,
    ActivityGroupRun,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkflowRun,
)
from ace_django.queue import ActivityLease

if TYPE_CHECKING:
    from datetime import datetime

    from ace.json_types import JsonValue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransactionalResult:
    """Result of transactional activity execution."""

    activity_id: str
    success: bool
    result: JsonValue = None
    error: str | None = None


class DjangoTransactionalActivityExecutor:
    """Executes transactional activities with same-database atomicity."""

    def execute(
        self,
        lease: ActivityLease,
        callable: Callable[[], JsonValue],
        *,
        now: datetime | None = None,
    ) -> TransactionalResult:
        """Execute a transactional activity.

        The callable and ACE completion commit together or roll back together.

        Args:
            lease: The activity lease from claim
            callable: Synchronous function returning the activity result
            now: Current time (for testing)

        Returns:
            TransactionalResult with success status and result/error
        """
        execute_at = now or timezone.now()

        try:
            with transaction.atomic():
                # Acquire locks in global order
                workflow, group, activity, attempt = self._acquire_locks(lease)

                # Verify execution mode
                if activity.execution_mode != ActivityExecutionMode.TRANSACTIONAL:
                    raise ValueError(f"Activity {activity.pk} is not TRANSACTIONAL mode")

                # Verify lease still valid
                if attempt.lease_expires_at <= execute_at:
                    raise ValueError("Lease expired before execution")

                # Execute the callable
                result = callable()

                # Complete the activity
                self._complete_activity(workflow, group, activity, attempt, result, execute_at)

                return TransactionalResult(
                    activity_id=str(activity.pk),
                    success=True,
                    result=result,
                )

        except Exception as exc:
            # All writes rolled back
            logger.warning(
                "Transactional activity %s failed: %s",
                lease.activity_run_id,
                exc,
            )
            return TransactionalResult(
                activity_id=lease.activity_run_id,
                success=False,
                error=str(exc)[:500],
            )

    def _acquire_locks(
        self,
        lease: ActivityLease,
    ) -> tuple[WorkflowRun | None, ActivityGroupRun | None, ActivityRun, ActivityAttempt]:
        """Acquire all ACE locks in global order."""
        # First, get activity IDs without locks
        activity = ActivityRun.objects.get(pk=lease.activity_run_id)

        workflow = None
        group = None

        # Lock workflow if present
        if activity.workflow_run_id:
            workflow = WorkflowRun.objects.select_for_update().get(pk=activity.workflow_run_id)

        # Lock group if present
        if activity.group_id:
            group = ActivityGroupRun.objects.select_for_update().get(pk=activity.group_id)

        # Lock activity
        locked_activity = ActivityRun.objects.select_for_update().get(pk=activity.pk)

        # Lock attempt
        attempt = ActivityAttempt.objects.select_for_update().get(
            activity_run=locked_activity,
            attempt_number=locked_activity.current_attempt,
            ownership_token=lease.ownership_token,
        )

        return workflow, group, locked_activity, attempt

    def _complete_activity(
        self,
        workflow: WorkflowRun | None,
        group: ActivityGroupRun | None,
        activity: ActivityRun,
        attempt: ActivityAttempt,
        result: JsonValue,
        now: datetime,
    ) -> None:
        """Complete activity and enqueue inbox event."""
        # Mark attempt succeeded
        attempt.status = AttemptStatus.SUCCEEDED
        attempt.completed_at = now
        attempt.save(update_fields=["status", "completed_at", "updated_at"])

        # Mark activity succeeded
        activity.status = ActivityStatus.SUCCEEDED
        activity.result = result
        activity.failure = None
        activity.available_at = None
        activity.completed_at = now
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

        # Handle group completion if applicable
        if group is not None:
            self._handle_group_completion(workflow, group, activity, result, now)
            return

        # Standalone activity - enqueue completion event
        if workflow is not None and not WorkflowStatus(workflow.status).is_terminal:
            enqueue_locked(
                workflow,
                source_type="activity",
                source_key=activity.activity_key,
                event_type=WorkflowEventType.ACTIVITY_COMPLETED,
                payload={
                    "activity_key": activity.activity_key,
                    "activity_run_id": str(activity.pk),
                    "result": result,
                },
                occurred_at=now,
            )

    def _handle_group_completion(
        self,
        workflow: WorkflowRun | None,
        group: ActivityGroupRun,
        completed_activity: ActivityRun,
        result: JsonValue,
        now: datetime,
    ) -> None:
        """Handle group fan-in for transactional activity."""
        from ace_django.models import ActivityGroupStatus

        if group.status != ActivityGroupStatus.RUNNING:
            # Group already terminal
            return

        # Check if all members are now succeeded
        members = list(group.members.order_by("activity_key"))
        if any(m.status != ActivityStatus.SUCCEEDED for m in members):
            # Not all done yet
            return

        # All members succeeded - build sorted result map
        results = {m.activity_key: m.result for m in members}
        group.status = ActivityGroupStatus.SUCCEEDED
        group.result = results
        group.completed_at = now
        group.save(update_fields=["status", "result", "completed_at", "updated_at"])

        # Enqueue group completion event
        if workflow is not None and not WorkflowStatus(workflow.status).is_terminal:
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
                occurred_at=now,
            )
