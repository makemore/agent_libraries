"""Client interface for durable workflow operations.

DjangoWorkflowClient provides methods for:
- Starting workflows with optional deadlines
- Sending durable signals to running workflows
- Requesting workflow cancellation

All operations are durable and idempotent via the inbox system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from ace import WorkflowEngine, WorkflowEventType, WorkflowSnapshot, WorkflowStatus
from django.db import transaction
from django.utils import timezone

from ace_django.exceptions import DuplicateInboxEvent
from ace_django.inbox import enqueue_locked
from ace_django.models import WorkflowRun

if TYPE_CHECKING:
    from datetime import datetime

    from ace import WorkflowRegistry
    from ace.json_types import JsonObject, JsonValue

    from ace_django.store import DjangoExecutionStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalReceipt:
    """Receipt for a successfully enqueued signal."""

    run_id: str
    signal_name: str
    inbox_sequence: int
    idempotent_retry: bool = False


@dataclass(frozen=True)
class CancellationReceipt:
    """Receipt for a successfully enqueued cancellation request."""

    run_id: str
    inbox_sequence: int
    idempotent_retry: bool = False


class DjangoWorkflowClient:
    """Client for durable workflow operations via the inbox system."""

    def __init__(
        self,
        registry: WorkflowRegistry,
        store: DjangoExecutionStore,
    ) -> None:
        self._registry = registry
        self._store = store

    def start(
        self,
        workflow_name: str,
        input: JsonObject,
        *,
        idempotency_key: str | None = None,
        deadline_at: datetime | None = None,
        now: datetime | None = None,
    ) -> WorkflowSnapshot:
        """Start a new workflow run with optional deadline.

        Args:
            workflow_name: Name of the workflow to start
            input: Workflow input data
            idempotency_key: Optional key for idempotent start
            deadline_at: Optional workflow-level deadline
            now: Current time (for testing)

        Returns:
            WorkflowSnapshot of the started workflow
        """
        from ace import SystemClock, UuidGenerator

        start_at = now or timezone.now()

        # Build engine for start
        engine = WorkflowEngine(
            registry=self._registry,
            store=self._store,
            clock=SystemClock() if now is None else _FixedClock(start_at),
            ids=UuidGenerator(),
        )

        # Start via engine (handles idempotency)
        snapshot = engine.start(
            workflow_name,
            input,
            idempotency_key=idempotency_key,
        )

        # Set deadline if provided
        if deadline_at is not None:
            with transaction.atomic():
                run = WorkflowRun.objects.select_for_update().get(pk=snapshot.run_id)
                run.deadline_at = deadline_at
                run.save(update_fields=["deadline_at", "updated_at"])

        return snapshot

    def signal(
        self,
        run_id: str,
        signal_name: str,
        payload: JsonValue = None,
        *,
        idempotency_key: str,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> SignalReceipt:
        """Send a durable signal to a workflow.

        Signals are durably enqueued and processed by the dispatcher.
        An identical signal (same idempotency_key) returns the same receipt.
        A different signal with the same key raises DuplicateInboxEvent.

        Args:
            run_id: Target workflow run ID
            signal_name: Name of the signal
            payload: Signal payload data
            idempotency_key: Required key for idempotent delivery
            actor: Optional actor identifier
            now: Current time (for testing)

        Returns:
            SignalReceipt with inbox sequence number

        Raises:
            DuplicateInboxEvent: If key exists with different payload
            WorkflowRun.DoesNotExist: If run not found
        """
        signal_at = now or timezone.now()

        with transaction.atomic():
            # Lock workflow run
            run = WorkflowRun.objects.select_for_update().get(pk=run_id)

            # Check if workflow can receive signals
            status = WorkflowStatus(run.status)
            if status.is_terminal:
                raise ValueError(f"Cannot signal terminal workflow (status={status.value})")
            if status == WorkflowStatus.BLOCKED:
                raise ValueError("Cannot signal blocked workflow")

            # Enqueue signal event
            inbox = enqueue_locked(
                run,
                source_type="signal",
                source_key=idempotency_key,
                event_type=WorkflowEventType.SIGNAL_RECEIVED,
                payload={"signal_name": signal_name, "payload": payload},
                actor=actor,
                occurred_at=signal_at,
            )

            # Check if this was an idempotent retry
            idempotent = inbox.occurred_at != signal_at

        return SignalReceipt(
            run_id=str(run.pk),
            signal_name=signal_name,
            inbox_sequence=inbox.inbox_sequence,
            idempotent_retry=idempotent,
        )

    def request_cancellation(
        self,
        run_id: str,
        reason: str,
        *,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> CancellationReceipt:
        """Request cancellation of a workflow.

        The cancellation request is durably enqueued. The workflow may
        choose to drain (CANCELLING) or cancel immediately. Already
        terminal workflows are ignored.

        Args:
            run_id: Target workflow run ID
            reason: Reason for cancellation
            actor: Optional actor identifier
            now: Current time (for testing)

        Returns:
            CancellationReceipt with inbox sequence number

        Raises:
            WorkflowRun.DoesNotExist: If run not found
        """
        cancel_at = now or timezone.now()

        with transaction.atomic():
            # Lock workflow run
            run = WorkflowRun.objects.select_for_update().get(pk=run_id)

            # Check if workflow can be cancelled
            status = WorkflowStatus(run.status)
            if status.is_terminal:
                # Already terminal - return success without enqueuing
                return CancellationReceipt(
                    run_id=str(run.pk),
                    inbox_sequence=0,
                    idempotent_retry=True,
                )

            if status == WorkflowStatus.BLOCKED:
                raise ValueError("Cannot cancel blocked workflow - use resume operation")

            # Enqueue cancellation event
            inbox = enqueue_locked(
                run,
                source_type="cancellation",
                source_key="requested",  # Only one cancellation per workflow
                event_type=WorkflowEventType.CANCELLATION_REQUESTED,
                payload={"reason": reason},
                actor=actor,
                occurred_at=cancel_at,
            )

            # Record cancellation timestamp on run if not already set
            if run.cancel_requested_at is None:
                run.cancel_requested_at = cancel_at
                run.save(update_fields=["cancel_requested_at", "updated_at"])

            idempotent = inbox.occurred_at != cancel_at

        return CancellationReceipt(
            run_id=str(run.pk),
            inbox_sequence=inbox.inbox_sequence,
            idempotent_retry=idempotent,
        )

    def get_snapshot(self, run_id: str) -> WorkflowSnapshot:
        """Get the current snapshot for a workflow run.

        Args:
            run_id: Workflow run ID

        Returns:
            Current WorkflowSnapshot

        Raises:
            WorkflowRun.DoesNotExist: If run not found
        """
        from ace_django.store import _to_snapshot

        run = WorkflowRun.objects.get(pk=run_id)
        return _to_snapshot(run)


class _FixedClock:
    """A clock that returns a fixed time (for testing)."""

    def __init__(self, fixed_time: datetime) -> None:
        self._fixed_time = fixed_time

    def now(self) -> datetime:
        return self._fixed_time
