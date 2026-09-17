"""Durable inbox operations for workflow events.

The inbox provides exactly-once delivery semantics for workflow events by:
1. Atomically committing source outcome (activity/timer) + inbox insertion
2. Using (workflow_run, source_type, source_key) uniqueness for idempotency
3. Allocating monotonic inbox_sequence for ordered dispatch
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from ace_django.exceptions import DuplicateInboxEvent
from ace_django.models import InboxStatus, WorkflowInboxEvent, WorkflowRun

if TYPE_CHECKING:
    from datetime import datetime

    from ace.json_types import JsonObject


def enqueue_locked(
    run: WorkflowRun,
    *,
    source_type: str,
    source_key: str,
    event_type: str,
    payload: JsonObject,
    actor: str | None = None,
    occurred_at: datetime | None = None,
) -> WorkflowInboxEvent:
    """Enqueue an inbox event while holding the workflow run lock.

    The caller must have already locked the WorkflowRun row via select_for_update().
    This function allocates the next inbox_sequence and creates the inbox event
    atomically within the caller's transaction.

    If a duplicate (source_type, source_key) already exists:
    - If payload/actor match, returns the existing event (idempotent retry)
    - If payload/actor differ, raises DuplicateInboxEvent

    Args:
        run: The locked WorkflowRun instance
        source_type: Source category (activity, group, timer, signal, deadline)
        source_key: Source identifier (activity_key, timer_key, signal idempotency key)
        event_type: The workflow event type to deliver
        payload: Event payload data
        actor: Optional actor identifier
        occurred_at: Event timestamp (defaults to now)

    Returns:
        The created or existing WorkflowInboxEvent

    Raises:
        DuplicateInboxEvent: If source already exists with different payload
    """
    occurred_at = occurred_at or timezone.now()

    # Check for existing event with same source
    existing = WorkflowInboxEvent.objects.filter(
        workflow_run=run,
        source_type=source_type,
        source_key=source_key,
    ).first()

    if existing is not None:
        # Idempotent retry: verify payload matches
        if (
            existing.event_type == event_type
            and existing.payload == payload
            and existing.actor == actor
        ):
            return existing
        raise DuplicateInboxEvent(
            f"Inbox event for {source_type}:{source_key} already exists with different payload."
        )

    # Allocate next sequence number
    next_sequence = run.last_inbox_sequence + 1
    run.last_inbox_sequence = next_sequence
    run.save(update_fields=["last_inbox_sequence", "updated_at"])

    # Create inbox event
    try:
        event = WorkflowInboxEvent.objects.create(
            workflow_run=run,
            inbox_sequence=next_sequence,
            source_type=source_type,
            source_key=source_key,
            event_type=event_type,
            payload=payload,
            actor=actor,
            occurred_at=occurred_at,
            status=InboxStatus.PENDING,
        )
        return event
    except IntegrityError as exc:
        # Race condition: another transaction created the same source
        existing = WorkflowInboxEvent.objects.filter(
            workflow_run=run,
            source_type=source_type,
            source_key=source_key,
        ).first()
        if existing is not None:
            if (
                existing.event_type == event_type
                and existing.payload == payload
                and existing.actor == actor
            ):
                return existing
        raise DuplicateInboxEvent(
            f"Inbox event for {source_type}:{source_key} created concurrently."
        ) from exc


def get_next_pending_inbox(run: WorkflowRun) -> WorkflowInboxEvent | None:
    """Get the lowest-sequence pending inbox event for dispatch.

    Returns None if no pending events exist or the run is terminal/blocked.
    """
    from ace import WorkflowStatus

    if WorkflowStatus(run.status).is_terminal or run.status == WorkflowStatus.BLOCKED.value:
        return None

    return (
        WorkflowInboxEvent.objects.filter(
            workflow_run=run,
            status__in=(InboxStatus.PENDING, InboxStatus.RETRYING),
        )
        .order_by("inbox_sequence")
        .first()
    )


def mark_processed(inbox: WorkflowInboxEvent, *, now: datetime | None = None) -> None:
    """Mark an inbox event as successfully processed."""
    processed_at = now or timezone.now()
    inbox.status = InboxStatus.PROCESSED
    inbox.processed_at = processed_at
    inbox.save(update_fields=["status", "processed_at", "updated_at"])


def mark_retrying(
    inbox: WorkflowInboxEvent,
    *,
    error_type: str,
    error_message: str,
    retry_delay_seconds: float,
    now: datetime | None = None,
) -> None:
    """Mark an inbox event for retry with exponential backoff."""
    from datetime import timedelta

    retry_at = (now or timezone.now()) + timedelta(seconds=retry_delay_seconds)
    inbox.status = InboxStatus.RETRYING
    inbox.attempts += 1
    inbox.available_at = retry_at
    inbox.last_error_type = error_type
    inbox.last_error_message = error_message[:2000]  # Truncate for safety
    inbox.save(
        update_fields=[
            "status",
            "attempts",
            "available_at",
            "last_error_type",
            "last_error_message",
            "updated_at",
        ]
    )


def mark_dead_letter(
    inbox: WorkflowInboxEvent,
    *,
    error_type: str,
    error_message: str,
    now: datetime | None = None,
) -> None:
    """Mark an inbox event as dead-lettered after exceeding retry budget."""
    dead_letter_at = now or timezone.now()
    inbox.status = InboxStatus.DEAD_LETTER
    inbox.dead_letter_at = dead_letter_at
    inbox.last_error_type = error_type
    inbox.last_error_message = error_message[:2000]
    inbox.save(
        update_fields=[
            "status",
            "dead_letter_at",
            "last_error_type",
            "last_error_message",
            "updated_at",
        ]
    )


def mark_discarded(
    inbox: WorkflowInboxEvent,
    *,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Mark an inbox event as discarded (e.g., workflow already terminal)."""
    discarded_at = now or timezone.now()
    inbox.status = InboxStatus.DISCARDED
    inbox.processed_at = discarded_at
    inbox.discard_reason = reason[:255]
    inbox.save(update_fields=["status", "processed_at", "discard_reason", "updated_at"])


def discard_remaining_inbox(run: WorkflowRun, reason: str, now: datetime | None = None) -> int:
    """Discard all remaining pending inbox events for a terminal workflow."""
    discarded_at = now or timezone.now()
    return WorkflowInboxEvent.objects.filter(
        workflow_run=run,
        status__in=(InboxStatus.PENDING, InboxStatus.RETRYING),
    ).update(
        status=InboxStatus.DISCARDED,
        processed_at=discarded_at,
        discard_reason=reason[:255],
        updated_at=discarded_at,
    )
