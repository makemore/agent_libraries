"""Tests for the workflow client.

Tests verify:
- Signal delivery and idempotency
- Cancellation requests
- Workflow start with deadlines
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz
from uuid import UUID

import pytest
from django.db import transaction

from ace import WorkflowEventType, WorkflowRegistry, WorkflowStatus
from ace_django.client import DjangoWorkflowClient, SignalReceipt, CancellationReceipt
from ace_django.exceptions import DuplicateInboxEvent
from ace_django.models import InboxStatus, WorkflowInboxEvent, WorkflowRun
from ace_django.store import DjangoExecutionStore
from ace_django.tests.helpers import AdapterWorkflow, FROZEN_NOW

pytestmark = pytest.mark.django_db(transaction=True)


def create_workflow_run(
    run_id: str = "10000000-0000-0000-0000-000000000001",
    status: str = "RUNNING",
) -> WorkflowRun:
    """Create a minimal workflow run for testing."""
    return WorkflowRun.objects.create(
        id=UUID(run_id),
        workflow_name="adapter-test",
        workflow_version="1",
        status=status,
        state={"phase": "running"},
        input={"test": True},
        idempotency_key=f"test-{run_id}",
        started_at=FROZEN_NOW,
    )


def build_client() -> DjangoWorkflowClient:
    """Build a client with the test registry."""
    registry = WorkflowRegistry()
    registry.register(AdapterWorkflow())
    store = DjangoExecutionStore()
    return DjangoWorkflowClient(registry, store)


def test_signal_creates_inbox_event() -> None:
    """signal() creates an inbox event for the signal."""
    run = create_workflow_run()
    client = build_client()

    receipt = client.signal(
        run_id=str(run.pk),
        signal_name="user_action",
        payload={"action": "approve"},
        idempotency_key="sig-001",
        now=FROZEN_NOW,
    )

    assert isinstance(receipt, SignalReceipt)
    assert receipt.run_id == str(run.pk)
    assert receipt.signal_name == "user_action"
    assert receipt.inbox_sequence == 1
    assert receipt.idempotent_retry is False

    # Verify inbox event
    inbox = WorkflowInboxEvent.objects.get(workflow_run=run)
    assert inbox.source_type == "signal"
    assert inbox.source_key == "sig-001"
    assert inbox.event_type == WorkflowEventType.SIGNAL_RECEIVED
    assert inbox.payload["signal_name"] == "user_action"
    assert inbox.payload["payload"]["action"] == "approve"


def test_signal_idempotent_same_payload() -> None:
    """signal() returns same receipt for identical signals."""
    run = create_workflow_run()
    client = build_client()

    receipt1 = client.signal(
        run_id=str(run.pk),
        signal_name="user_action",
        payload={"action": "approve"},
        idempotency_key="sig-001",
        now=FROZEN_NOW,
    )

    receipt2 = client.signal(
        run_id=str(run.pk),
        signal_name="user_action",
        payload={"action": "approve"},
        idempotency_key="sig-001",
        now=FROZEN_NOW + timedelta(seconds=10),
    )

    assert receipt1.inbox_sequence == receipt2.inbox_sequence
    assert receipt2.idempotent_retry is True
    assert WorkflowInboxEvent.objects.filter(workflow_run=run).count() == 1


def test_signal_rejects_different_payload() -> None:
    """signal() raises for same key with different payload."""
    run = create_workflow_run()
    client = build_client()

    client.signal(
        run_id=str(run.pk),
        signal_name="user_action",
        payload={"action": "approve"},
        idempotency_key="sig-001",
        now=FROZEN_NOW,
    )

    with pytest.raises(DuplicateInboxEvent):
        client.signal(
            run_id=str(run.pk),
            signal_name="user_action",
            payload={"action": "reject"},  # Different payload
            idempotency_key="sig-001",
            now=FROZEN_NOW,
        )


def test_signal_rejects_terminal_workflow() -> None:
    """signal() raises for terminal workflows."""
    run = create_workflow_run(status=WorkflowStatus.COMPLETED.value)
    client = build_client()

    with pytest.raises(ValueError, match="terminal"):
        client.signal(
            run_id=str(run.pk),
            signal_name="user_action",
            payload={},
            idempotency_key="sig-001",
            now=FROZEN_NOW,
        )


def test_request_cancellation_creates_event() -> None:
    """request_cancellation() creates inbox event."""
    run = create_workflow_run()
    client = build_client()

    receipt = client.request_cancellation(
        run_id=str(run.pk),
        reason="User requested cancellation",
        now=FROZEN_NOW,
    )

    assert isinstance(receipt, CancellationReceipt)
    assert receipt.run_id == str(run.pk)
    assert receipt.idempotent_retry is False

    # Verify inbox event
    inbox = WorkflowInboxEvent.objects.get(workflow_run=run)
    assert inbox.source_type == "cancellation"
    assert inbox.event_type == WorkflowEventType.CANCELLATION_REQUESTED

    # Verify cancel_requested_at is set
    run.refresh_from_db()
    assert run.cancel_requested_at == FROZEN_NOW
