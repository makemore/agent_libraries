"""Activity timeout enforcement service.

This service monitors running activities for timeout violations:
- schedule_to_close: Total time from scheduling to completion (across all attempts)
- start_to_close: Time from claim to completion (per attempt)
- heartbeat: Time since last heartbeat (per attempt)

When a timeout is exceeded:
1. The current attempt is marked TIMED_OUT
2. If retries remain within schedule_to_close, activity is retried
3. Otherwise, activity is failed and an inbox event is enqueued
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from ace import WorkflowEventType, WorkflowStatus
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ace_django.inbox import enqueue_locked
from ace_django.models import (
    ActivityAttempt,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkflowRun,
)
from ace_django.retry import retry_policy_from_dict

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimeoutResult:
    """Result of checking/enforcing a timeout."""

    activity_id: str
    run_id: str | None
    timeout_type: str  # schedule_to_close, start_to_close, heartbeat
    retried: bool = False
    failed: bool = False
    error: str | None = None


class DjangoActivityTimeoutService:
    """Enforces activity timeout policies."""

    def enforce_once(self, now: datetime | None = None) -> TimeoutResult | None:
        """Check for and enforce one activity timeout.

        Returns None if no timeouts are due.
        """
        enforce_at = now or timezone.now()

        # Check schedule-to-close first (whole activity)
        result = self._check_schedule_to_close(enforce_at)
        if result is not None:
            return result

        # Check start-to-close and heartbeat (current attempt)
        result = self._check_attempt_timeouts(enforce_at)
        if result is not None:
            return result

        return None

    def _check_schedule_to_close(self, now: datetime) -> TimeoutResult | None:
        """Check for activities that exceeded schedule_to_close deadline."""
        # Find activity with exceeded schedule_to_close_at
        candidate = (
            ActivityRun.objects.filter(
                status__in=[ActivityStatus.READY, ActivityStatus.RUNNING, ActivityStatus.RETRYING],
                schedule_to_close_at__lte=now,
                schedule_to_close_at__isnull=False,
            )
            .exclude(
                workflow_run__status__in=[
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.FAILED.value,
                    WorkflowStatus.CANCELLED.value,
                    WorkflowStatus.BLOCKED.value,
                ]
            )
            .order_by("schedule_to_close_at")
            .first()
        )

        if candidate is None:
            return None

        return self._fail_activity_timeout(candidate, "schedule_to_close", now)

    def _check_attempt_timeouts(self, now: datetime) -> TimeoutResult | None:
        """Check for attempts that exceeded start_to_close or heartbeat timeout."""
        # Find running activities with timeout metadata
        candidate = (
            ActivityRun.objects.filter(
                status=ActivityStatus.RUNNING,
            )
            .filter(Q(start_to_close_seconds__isnull=False) | Q(heartbeat_seconds__isnull=False))
            .exclude(
                workflow_run__status__in=[
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.FAILED.value,
                    WorkflowStatus.CANCELLED.value,
                    WorkflowStatus.BLOCKED.value,
                ]
            )
            .order_by("created_at")
            .first()
        )

        if candidate is None:
            return None

        # Get current attempt
        attempt = ActivityAttempt.objects.filter(
            activity_run=candidate,
            attempt_number=candidate.current_attempt,
            status=AttemptStatus.RUNNING,
        ).first()

        if attempt is None:
            return None

        # Check start_to_close
        if candidate.start_to_close_seconds is not None:
            deadline = attempt.started_at + timedelta(seconds=candidate.start_to_close_seconds)
            if now >= deadline:
                return self._timeout_attempt(candidate, attempt, "start_to_close", now)

        # Check heartbeat
        if candidate.heartbeat_seconds is not None and attempt.heartbeat_deadline is not None:
            if now >= attempt.heartbeat_deadline:
                return self._timeout_attempt(candidate, attempt, "heartbeat", now)

        return None

    def _timeout_attempt(
        self,
        activity: ActivityRun,
        attempt: ActivityAttempt,
        timeout_type: str,
        now: datetime,
    ) -> TimeoutResult:
        """Mark attempt as timed out and potentially retry."""
        with transaction.atomic():
            # Lock workflow if present
            workflow = None
            if activity.workflow_run_id:
                workflow = WorkflowRun.objects.select_for_update().get(pk=activity.workflow_run_id)
                if WorkflowStatus(workflow.status).is_terminal:
                    return TimeoutResult(
                        activity_id=str(activity.pk),
                        run_id=str(workflow.pk),
                        timeout_type=timeout_type,
                        error="Workflow is terminal",
                    )

            # Lock activity
            locked_activity = ActivityRun.objects.select_for_update().get(pk=activity.pk)

            # Lock attempt
            locked_attempt = ActivityAttempt.objects.select_for_update().get(pk=attempt.pk)

            # Mark attempt timed out
            locked_attempt.status = AttemptStatus.TIMED_OUT
            locked_attempt.completed_at = now
            locked_attempt.timeout_cause = timeout_type
            locked_attempt.save(
                update_fields=["status", "completed_at", "timeout_cause", "updated_at"]
            )

            # Check if can retry (within schedule_to_close and retry policy)
            can_retry = self._can_retry(locked_activity, now)

            if can_retry:
                return self._retry_activity(locked_activity, timeout_type, now, workflow)
            else:
                return self._fail_activity(locked_activity, timeout_type, now, workflow)

    def _can_retry(self, activity: ActivityRun, now: datetime) -> bool:
        """Check if activity can be retried."""
        # Check schedule_to_close deadline
        if activity.schedule_to_close_at is not None and now >= activity.schedule_to_close_at:
            return False

        # Check retry policy
        retry_policy = activity.retry_policy or {}
        max_attempts = retry_policy.get("max_attempts", 1)
        if activity.current_attempt >= max_attempts:
            return False

        return True

    def _retry_activity(
        self,
        activity: ActivityRun,
        timeout_type: str,
        now: datetime,
        workflow: WorkflowRun | None,
    ) -> TimeoutResult:
        """Schedule activity for retry."""
        policy = retry_policy_from_dict(activity.retry_policy)
        delay = policy.delay_after(activity.current_attempt)
        available_at = now + timedelta(seconds=delay)

        activity.status = ActivityStatus.RETRYING
        activity.available_at = available_at
        activity.save(update_fields=["status", "available_at", "updated_at"])

        logger.info(
            "Activity %s %s timeout, retrying in %.1fs",
            activity.pk,
            timeout_type,
            delay,
        )

        return TimeoutResult(
            activity_id=str(activity.pk),
            run_id=str(workflow.pk) if workflow else None,
            timeout_type=timeout_type,
            retried=True,
        )

    def _fail_activity(
        self,
        activity: ActivityRun,
        timeout_type: str,
        now: datetime,
        workflow: WorkflowRun | None,
    ) -> TimeoutResult:
        """Fail the activity and enqueue inbox event."""
        failure = {
            "error_type": "ActivityTimeout",
            "message": f"Activity timed out ({timeout_type})",
            "details": {"timeout_type": timeout_type},
        }

        activity.status = ActivityStatus.FAILED
        activity.failure = failure
        activity.completed_at = now
        activity.save(update_fields=["status", "failure", "completed_at", "updated_at"])

        # Enqueue failure event if part of workflow
        if workflow is not None:
            enqueue_locked(
                workflow,
                source_type="activity",
                source_key=activity.activity_key,
                event_type=WorkflowEventType.ACTIVITY_FAILED,
                payload={
                    "activity_key": activity.activity_key,
                    "activity_run_id": str(activity.pk),
                    "failure": failure,
                },
                occurred_at=now,
            )

        logger.warning(
            "Activity %s failed due to %s timeout",
            activity.pk,
            timeout_type,
        )

        return TimeoutResult(
            activity_id=str(activity.pk),
            run_id=str(workflow.pk) if workflow else None,
            timeout_type=timeout_type,
            failed=True,
        )

    def _fail_activity_timeout(
        self,
        activity: ActivityRun,
        timeout_type: str,
        now: datetime,
    ) -> TimeoutResult:
        """Fail activity due to schedule_to_close timeout."""
        with transaction.atomic():
            workflow = None
            if activity.workflow_run_id:
                workflow = WorkflowRun.objects.select_for_update().get(pk=activity.workflow_run_id)

            locked_activity = ActivityRun.objects.select_for_update().get(pk=activity.pk)
            return self._fail_activity(locked_activity, timeout_type, now, workflow)

    def run(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        max_iterations: int | None = None,
        stop_event: object | None = None,
    ) -> int:
        """Run the timeout enforcement polling loop."""
        import time

        enforced = 0
        iterations = 0

        while True:
            if max_iterations is not None and iterations >= max_iterations:
                break
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break

            result = self.enforce_once()
            if result is not None:
                enforced += 1
                continue

            time.sleep(poll_interval_seconds)
            iterations += 1

        return enforced
