"""Durable workflow dispatcher for processing inbox events.

The dispatcher processes inbox events in sequence order, ensuring:
1. Events are processed in strict inbox_sequence order (no overtaking)
2. Transition failures are recorded and retried with exponential backoff
3. After max retries, the workflow is BLOCKED and events are dead-lettered
4. Successful transitions atomically commit snapshot + event + commands + inbox ack
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ace import RetryPolicy, WorkflowEngine, WorkflowEvent, WorkflowEventType, WorkflowStatus
from ace.codec import serialize_commands
from django.db import transaction
from django.utils import timezone

from ace_django.exceptions import DispatcherError, TransitionFailure, WorkflowBlocked
from ace_django.inbox import (
    discard_remaining_inbox,
    get_next_pending_inbox,
    mark_dead_letter,
    mark_discarded,
    mark_processed,
    mark_retrying,
)
from ace_django.models import (
    InboxStatus,
    WorkflowEvent as WorkflowEventModel,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTransitionFailure,
)

if TYPE_CHECKING:
    from datetime import datetime

    from ace import WorkflowRegistry

    from ace_django.store import DjangoExecutionStore

logger = logging.getLogger(__name__)

# Default retry configuration
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_MAX_DELAY_SECONDS = 300.0
# Transition retries are driven by inbox event processing order across many
# workflows sharing the same dispatcher; a shared upstream blip (DB
# contention, a flaky downstream call) can fail a whole batch in the same
# tick. Jitter spreads their retries instead of releasing them all at once.
DEFAULT_JITTER_FRACTION = 0.2


@dataclass(frozen=True)
class DispatchResult:
    """Result of dispatching a single inbox event."""

    run_id: str
    inbox_sequence: int
    success: bool
    blocked: bool = False
    discarded: bool = False
    error: str | None = None


class DjangoWorkflowDispatcher:
    """Dispatches inbox events to workflow transitions."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        store: DjangoExecutionStore,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        jitter_fraction: float = DEFAULT_JITTER_FRACTION,
    ) -> None:
        self._registry = registry
        self._store = store
        self._max_attempts = max_attempts
        self._retry_policy = RetryPolicy(
            max_attempts=max_attempts,
            initial_delay_seconds=initial_delay_seconds,
            backoff_multiplier=backoff_multiplier,
            max_delay_seconds=max_delay_seconds,
            jitter_fraction=jitter_fraction,
        )

    def dispatch_once(self, now: datetime | None = None) -> DispatchResult | None:
        """Process the next available inbox event.

        Returns None if no events are ready for dispatch.
        """
        dispatch_at = now or timezone.now()

        # Find candidate runs with pending inbox events
        candidate = self._find_candidate(dispatch_at)
        if candidate is None:
            return None

        return self._dispatch_event(candidate, dispatch_at)

    def _find_candidate(self, now: datetime) -> WorkflowInboxEvent | None:
        """Find the next inbox event ready for dispatch.

        Runs pinned to workflow versions this dispatcher does not support are
        skipped entirely: their events stay PENDING without spending recovery
        budget, and other runs are not starved.
        """
        from django.db.models import Q

        # Find runs with pending events that are available now
        available_filter = Q(available_at__isnull=True) | Q(available_at__lte=now)

        # Version routing: only dispatch runs with a registered definition.
        supported_filter = Q(pk__in=[])
        for name, version in self._registry.identities():
            supported_filter |= Q(
                workflow_run__workflow_name=name,
                workflow_run__workflow_version=version,
            )

        return (
            WorkflowInboxEvent.objects.filter(
                status__in=(InboxStatus.PENDING, InboxStatus.RETRYING),
            )
            .filter(available_filter)
            .filter(supported_filter)
            .exclude(
                workflow_run__status__in=[
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.FAILED.value,
                    WorkflowStatus.CANCELLED.value,
                    WorkflowStatus.BLOCKED.value,
                ]
            )
            .order_by("workflow_run_id", "inbox_sequence")
            .first()
        )

    def _dispatch_event(
        self,
        candidate: WorkflowInboxEvent,
        now: datetime,
    ) -> DispatchResult:
        """Dispatch a single inbox event with proper locking and error handling."""
        with transaction.atomic():
            # Lock the workflow run first (global lock order)
            run = WorkflowRun.objects.select_for_update().get(pk=candidate.workflow_run_id)

            # Re-check run status under lock
            status = WorkflowStatus(run.status)
            if status.is_terminal:
                mark_discarded(candidate, reason=f"Workflow is {status.value}", now=now)
                discard_remaining_inbox(run, f"Workflow is {status.value}", now)
                return DispatchResult(
                    run_id=str(run.pk),
                    inbox_sequence=candidate.inbox_sequence,
                    success=False,
                    discarded=True,
                )

            if status == WorkflowStatus.BLOCKED:
                return DispatchResult(
                    run_id=str(run.pk),
                    inbox_sequence=candidate.inbox_sequence,
                    success=False,
                    blocked=True,
                    error="Workflow is blocked",
                )

            # Lock inbox event
            inbox = WorkflowInboxEvent.objects.select_for_update().get(pk=candidate.pk)

            # Verify this is the next expected sequence (no overtaking)
            if inbox.inbox_sequence != self._get_next_inbox_sequence(run):
                return DispatchResult(
                    run_id=str(run.pk),
                    inbox_sequence=inbox.inbox_sequence,
                    success=False,
                    error="Event sequence mismatch - out of order dispatch",
                )

            # Check workflow version support
            if not self._registry.has(run.workflow_name, run.workflow_version):
                logger.warning(
                    "Dispatcher skipping run %s - workflow %s:%s not registered",
                    run.pk,
                    run.workflow_name,
                    run.workflow_version,
                )
                return DispatchResult(
                    run_id=str(run.pk),
                    inbox_sequence=inbox.inbox_sequence,
                    success=False,
                    error=f"Workflow {run.workflow_name}:{run.workflow_version} not registered",
                )

            # Attempt transition within a savepoint
            try:
                return self._apply_transition(run, inbox, now)
            except Exception as exc:
                return self._handle_failure(run, inbox, exc, now)

    def _apply_transition(
        self,
        run: WorkflowRun,
        inbox: WorkflowInboxEvent,
        now: datetime,
    ) -> DispatchResult:
        """Apply the workflow transition within a savepoint."""
        from ace_django.store import _to_snapshot

        # Build core event from inbox
        next_seq = run.last_event_sequence + 1
        core_event = WorkflowEvent(
            event_id=str(inbox.pk),  # Use inbox ID as event ID
            run_id=str(run.pk),
            sequence=next_seq,
            event_type=inbox.event_type,
            occurred_at=inbox.occurred_at,
            payload=inbox.payload,
            actor=inbox.actor,
        )

        # Load current snapshot
        snapshot = _to_snapshot(run)

        # Resolve workflow definition
        definition = self._registry.resolve(run.workflow_name, run.workflow_version)

        # Build engine with no-op store (we handle persistence directly)
        from ace import SystemClock, UuidGenerator

        engine = WorkflowEngine(
            registry=self._registry,
            store=self._store,
            clock=SystemClock(),
            ids=UuidGenerator(),
        )

        # Apply event using engine's apply_event
        with transaction.atomic():
            updated_snapshot, commands = engine.apply_event(snapshot, core_event)

            # Persist event with commands
            WorkflowEventModel.objects.create(
                id=inbox.pk,  # Use inbox UUID as event UUID
                workflow_run=run,
                sequence=next_seq,
                event_type=inbox.event_type,
                payload=inbox.payload,
                actor=inbox.actor,
                occurred_at=inbox.occurred_at,
                commands=serialize_commands(commands),
            )

            # Update workflow run
            run.status = updated_snapshot.status.value
            run.state = updated_snapshot.state
            run.result = updated_snapshot.result
            run.failure = (
                {
                    "error_type": updated_snapshot.failure.error_type,
                    "message": updated_snapshot.failure.message,
                    "details": updated_snapshot.failure.details,
                }
                if updated_snapshot.failure
                else None
            )
            run.last_event_sequence = next_seq
            if updated_snapshot.status.is_terminal:
                run.completed_at = now
            run.save(
                update_fields=[
                    "status",
                    "state",
                    "result",
                    "failure",
                    "last_event_sequence",
                    "completed_at",
                    "updated_at",
                ]
            )

            # Materialize commands
            from ace_django.store import _materialize_commands

            _materialize_commands(run, commands, now)

            # Mark inbox processed
            mark_processed(inbox, now=now)

            # If terminal, discard remaining inbox events
            if updated_snapshot.status.is_terminal:
                discard_remaining_inbox(
                    run, f"Workflow completed: {updated_snapshot.status.value}", now
                )

        return DispatchResult(
            run_id=str(run.pk),
            inbox_sequence=inbox.inbox_sequence,
            success=True,
        )

    def _handle_failure(
        self,
        run: WorkflowRun,
        inbox: WorkflowInboxEvent,
        exc: Exception,
        now: datetime,
    ) -> DispatchResult:
        """Handle transition failure with retry or blocking."""
        error_type = type(exc).__name__
        error_message = str(exc)

        # Record failure
        WorkflowTransitionFailure.objects.create(
            workflow_run=run,
            inbox_event=inbox,
            attempt=inbox.attempts + 1,
            error_type=error_type,
            error_message=error_message[:2000],
            error_details={},
        )

        # Check if we've exhausted retries
        if inbox.attempts + 1 >= self._max_attempts:
            # Block workflow and dead-letter event
            return self._block_workflow(run, inbox, error_type, error_message, now)

        # Schedule retry with jittered exponential backoff.
        delay = self._retry_policy.delay_after(inbox.attempts + 1)
        mark_retrying(
            inbox,
            error_type=error_type,
            error_message=error_message,
            retry_delay_seconds=delay,
            now=now,
        )

        logger.warning(
            "Transition failed for run %s inbox %d, retrying in %.1fs: %s",
            run.pk,
            inbox.inbox_sequence,
            delay,
            error_message,
        )

        return DispatchResult(
            run_id=str(run.pk),
            inbox_sequence=inbox.inbox_sequence,
            success=False,
            error=error_message,
        )

    def _block_workflow(
        self,
        run: WorkflowRun,
        inbox: WorkflowInboxEvent,
        error_type: str,
        error_message: str,
        now: datetime,
    ) -> DispatchResult:
        """Block workflow after exhausting retry budget."""
        # Dead-letter the inbox event
        mark_dead_letter(inbox, error_type=error_type, error_message=error_message, now=now)

        # Record resolution
        WorkflowTransitionFailure.objects.filter(
            workflow_run=run,
            inbox_event=inbox,
        ).update(resolution="blocked")

        # Block the workflow
        run.blocked_at = now
        run.blocked_from_status = run.status
        run.status = WorkflowStatus.BLOCKED.value
        run.block_reason = (
            f"Transition failed after {self._max_attempts} attempts: {error_message[:500]}"
        )
        run.save(
            update_fields=[
                "status",
                "blocked_at",
                "blocked_from_status",
                "block_reason",
                "updated_at",
            ]
        )

        logger.error(
            "Workflow %s blocked after %d failed attempts: %s",
            run.pk,
            self._max_attempts,
            error_message,
        )

        return DispatchResult(
            run_id=str(run.pk),
            inbox_sequence=inbox.inbox_sequence,
            success=False,
            blocked=True,
            error=error_message,
        )

    def _get_next_inbox_sequence(self, run: WorkflowRun) -> int:
        """Get the sequence of the next inbox event to process."""
        next_inbox = get_next_pending_inbox(run)
        return next_inbox.inbox_sequence if next_inbox else -1
