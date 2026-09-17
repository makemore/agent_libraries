"""Durable timer service for scheduled workflow events.

The timer service polls for SCHEDULED timers that are due, then atomically:
1. Locks the workflow run (respecting global lock order)
2. Locks the timer
3. Creates an inbox event for the timer
4. Marks the timer as DELIVERED

Timer-vs-cancellation race is decided by the WorkflowRun lock and inbox sequence:
- If cancellation is already committed, the timer is discarded
- If the timer wins, it atomically marks DELIVERED and enqueues
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ace import WorkflowEventType, WorkflowStatus
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ace_django.inbox import enqueue_locked, mark_discarded
from ace_django.models import (
    InboxStatus,
    TimerStatus,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTimer,
)

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

# Default retry configuration for timer delivery
DEFAULT_MAX_DELIVERY_ATTEMPTS = 5
DEFAULT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_MAX_DELAY_SECONDS = 300.0


@dataclass(frozen=True)
class TimerDeliveryResult:
    """Result of attempting to deliver a timer."""

    timer_id: str
    run_id: str
    success: bool
    delivered: bool = False
    discarded: bool = False
    retrying: bool = False
    blocked: bool = False
    error: str | None = None


class DjangoTimerService:
    """Delivers scheduled timers to the workflow inbox."""

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    ) -> None:
        self._max_attempts = max_attempts
        self._initial_delay_seconds = initial_delay_seconds
        self._backoff_multiplier = backoff_multiplier
        self._max_delay_seconds = max_delay_seconds

    def deliver_once(self, now: datetime | None = None) -> TimerDeliveryResult | None:
        """Attempt to deliver the next due timer.

        Returns None if no timers are ready for delivery.
        """
        deliver_at = now or timezone.now()

        # Find candidate timer
        candidate = self._find_candidate(deliver_at)
        if candidate is None:
            return None

        return self._deliver_timer(candidate, deliver_at)

    def _find_candidate(self, now: datetime) -> WorkflowTimer | None:
        """Find the next timer ready for delivery."""
        # Scheduled timers that are due
        scheduled_filter = Q(status=TimerStatus.SCHEDULED, fire_at__lte=now)
        # Retrying timers that are due
        retry_filter = Q(status=TimerStatus.RETRYING, next_attempt_at__lte=now)

        return (
            WorkflowTimer.objects.filter(scheduled_filter | retry_filter)
            .exclude(
                workflow_run__status__in=[
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.FAILED.value,
                    WorkflowStatus.CANCELLED.value,
                    WorkflowStatus.BLOCKED.value,
                ]
            )
            .order_by("fire_at")
            .first()
        )

    def _deliver_timer(
        self,
        candidate: WorkflowTimer,
        now: datetime,
    ) -> TimerDeliveryResult:
        """Deliver a single timer with proper locking."""
        try:
            with transaction.atomic():
                # Lock workflow run first (global lock order)
                run = WorkflowRun.objects.select_for_update().get(pk=candidate.workflow_run_id)

                # Check if workflow is terminal
                status = WorkflowStatus(run.status)
                if status.is_terminal:
                    # Timer cannot fire - workflow already done
                    self._cancel_timer(candidate, f"Workflow is {status.value}", now)
                    return TimerDeliveryResult(
                        timer_id=str(candidate.pk),
                        run_id=str(run.pk),
                        success=False,
                        discarded=True,
                    )

                if status == WorkflowStatus.BLOCKED:
                    return TimerDeliveryResult(
                        timer_id=str(candidate.pk),
                        run_id=str(run.pk),
                        success=False,
                        blocked=True,
                        error="Workflow is blocked",
                    )

                # Lock timer
                timer = WorkflowTimer.objects.select_for_update().get(pk=candidate.pk)

                # Verify timer is still deliverable
                if timer.status not in (TimerStatus.SCHEDULED, TimerStatus.RETRYING):
                    return TimerDeliveryResult(
                        timer_id=str(timer.pk),
                        run_id=str(run.pk),
                        success=False,
                        discarded=True,
                    )

                # Create inbox event for timer
                return self._create_timer_inbox_event(run, timer, now)

        except Exception as exc:
            return self._handle_delivery_failure(candidate, exc, now)

    def _create_timer_inbox_event(
        self,
        run: WorkflowRun,
        timer: WorkflowTimer,
        now: datetime,
    ) -> TimerDeliveryResult:
        """Create inbox event and mark timer delivered atomically."""
        # Enqueue to inbox
        enqueue_locked(
            run,
            source_type="timer",
            source_key=timer.timer_key,
            event_type=WorkflowEventType.TIMER_FIRED,
            payload={
                "timer_key": timer.timer_key,
                "fire_at": timer.fire_at.isoformat(),
                "payload": timer.payload,
            },
            occurred_at=now,
        )

        # Mark timer as delivered
        timer.status = TimerStatus.DELIVERED
        timer.delivered_at = now
        timer.save(update_fields=["status", "delivered_at", "updated_at"])

        logger.info(
            "Timer %s delivered for workflow %s",
            timer.timer_key,
            run.pk,
        )

        return TimerDeliveryResult(
            timer_id=str(timer.pk),
            run_id=str(run.pk),
            success=True,
            delivered=True,
        )

    def _cancel_timer(
        self,
        timer: WorkflowTimer,
        reason: str,
        now: datetime,
    ) -> None:
        """Cancel a timer that cannot be delivered."""
        timer.status = TimerStatus.CANCELLED
        timer.cancelled_at = now
        timer.last_error = reason[:500]
        timer.save(update_fields=["status", "cancelled_at", "last_error", "updated_at"])

    def _handle_delivery_failure(
        self,
        timer: WorkflowTimer,
        exc: Exception,
        now: datetime,
    ) -> TimerDeliveryResult:
        """Handle timer delivery failure with retry/DLQ."""
        from datetime import timedelta

        error_message = str(exc)[:500]
        timer.refresh_from_db()

        # Increment attempts
        timer.delivery_attempts += 1

        if timer.delivery_attempts >= self._max_attempts:
            # Dead letter the timer
            timer.status = TimerStatus.DEAD_LETTER
            timer.last_error = error_message
            timer.save(
                update_fields=[
                    "status",
                    "delivery_attempts",
                    "last_error",
                    "updated_at",
                ]
            )

            logger.error(
                "Timer %s dead-lettered after %d attempts: %s",
                timer.pk,
                timer.delivery_attempts,
                error_message,
            )

            return TimerDeliveryResult(
                timer_id=str(timer.pk),
                run_id=str(timer.workflow_run_id),
                success=False,
                blocked=True,
                error=error_message,
            )

        # Schedule retry with exponential backoff
        delay = min(
            self._initial_delay_seconds
            * (self._backoff_multiplier ** (timer.delivery_attempts - 1)),
            self._max_delay_seconds,
        )
        timer.status = TimerStatus.RETRYING
        timer.next_attempt_at = now + timedelta(seconds=delay)
        timer.last_error = error_message
        timer.save(
            update_fields=[
                "status",
                "delivery_attempts",
                "next_attempt_at",
                "last_error",
                "updated_at",
            ]
        )

        logger.warning(
            "Timer %s delivery failed, retrying in %.1fs: %s",
            timer.pk,
            delay,
            error_message,
        )

        return TimerDeliveryResult(
            timer_id=str(timer.pk),
            run_id=str(timer.workflow_run_id),
            success=False,
            retrying=True,
            error=error_message,
        )

    def run(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        max_iterations: int | None = None,
        stop_event: object | None = None,
    ) -> int:
        """Run the timer service polling loop.

        Returns the number of timers delivered.
        """
        import time

        delivered = 0
        iterations = 0

        while True:
            if max_iterations is not None and iterations >= max_iterations:
                break
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                break

            result = self.deliver_once()
            if result is not None:
                if result.delivered:
                    delivered += 1
                # Continue immediately if we found work
                continue

            # No work found, sleep
            time.sleep(poll_interval_seconds)
            iterations += 1

        return delivered
