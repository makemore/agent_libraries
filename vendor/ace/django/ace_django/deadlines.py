"""Deadline service for workflow timeout enforcement.

The deadline service monitors workflows with deadline_at set and emits
WORKFLOW_DEADLINE_EXCEEDED events when the deadline passes. The dispatcher
then commits the built-in failure and cancellation of outstanding work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ace import WorkflowEventType, WorkflowStatus
from django.db import transaction
from django.utils import timezone

from ace_django.inbox import enqueue_locked
from ace_django.models import WorkflowRun

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeadlineEnforcementResult:
    """Result of enforcing a workflow deadline."""

    run_id: str
    success: bool
    exceeded: bool = False
    already_terminal: bool = False
    error: str | None = None


class DjangoDeadlineService:
    """Enforces workflow deadlines by emitting deadline exceeded events."""

    def enforce_once(self, now: datetime | None = None) -> DeadlineEnforcementResult | None:
        """Check for and enforce one exceeded deadline.

        Returns None if no deadlines are due.
        """
        enforce_at = now or timezone.now()

        # Find workflow with exceeded deadline
        candidate = self._find_candidate(enforce_at)
        if candidate is None:
            return None

        return self._enforce_deadline(candidate, enforce_at)

    def _find_candidate(self, now: datetime) -> WorkflowRun | None:
        """Find workflow with exceeded deadline."""
        return (
            WorkflowRun.objects.filter(
                deadline_at__lte=now,
                deadline_at__isnull=False,
                status__in=[
                    WorkflowStatus.RUNNING.value,
                    WorkflowStatus.WAITING.value,
                    WorkflowStatus.CANCELLING.value,
                ],
            )
            .order_by("deadline_at")
            .first()
        )

    def _enforce_deadline(
        self,
        candidate: WorkflowRun,
        now: datetime,
    ) -> DeadlineEnforcementResult:
        """Enforce deadline by emitting event."""
        try:
            with transaction.atomic():
                # Lock workflow run
                run = WorkflowRun.objects.select_for_update().get(pk=candidate.pk)

                # Re-check under lock
                status = WorkflowStatus(run.status)
                if status.is_terminal:
                    return DeadlineEnforcementResult(
                        run_id=str(run.pk),
                        success=False,
                        already_terminal=True,
                    )

                if status == WorkflowStatus.BLOCKED:
                    return DeadlineEnforcementResult(
                        run_id=str(run.pk),
                        success=False,
                        error="Workflow is blocked",
                    )

                # Check deadline again under lock
                if run.deadline_at is None or run.deadline_at > now:
                    return DeadlineEnforcementResult(
                        run_id=str(run.pk),
                        success=False,
                        error="Deadline no longer exceeded",
                    )

                # Enqueue deadline exceeded event
                enqueue_locked(
                    run,
                    source_type="deadline",
                    source_key="workflow_deadline",
                    event_type=WorkflowEventType.WORKFLOW_DEADLINE_EXCEEDED,
                    payload={
                        "deadline_at": run.deadline_at.isoformat(),
                        "exceeded_at": now.isoformat(),
                    },
                    occurred_at=now,
                )

                # Clear deadline to prevent re-processing
                run.deadline_at = None
                run.save(update_fields=["deadline_at", "updated_at"])

                logger.info("Deadline exceeded for workflow %s", run.pk)

                return DeadlineEnforcementResult(
                    run_id=str(run.pk),
                    success=True,
                    exceeded=True,
                )

        except Exception as exc:
            logger.error(
                "Error enforcing deadline for workflow %s: %s",
                candidate.pk,
                exc,
            )
            return DeadlineEnforcementResult(
                run_id=str(candidate.pk),
                success=False,
                error=str(exc)[:500],
            )

    def run(
        self,
        *,
        poll_interval_seconds: float = 1.0,
        max_iterations: int | None = None,
        stop_event: object | None = None,
    ) -> int:
        """Run the deadline service polling loop.

        Returns the number of deadlines enforced.
        """
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
                if result.exceeded:
                    enforced += 1
                # Continue immediately if we found work
                continue

            # No work found, sleep
            time.sleep(poll_interval_seconds)
            iterations += 1

        return enforced
