"""Head-of-line and stale-candidate regressions using isolated database state.

Race tests deterministically interleave two dispatchers at the non-locking
candidate boundary. They exercise revalidation, not PostgreSQL lock contention.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from ace import WorkflowRegistry, WorkflowStatus
from ace_django.client import DjangoWorkflowClient
from ace_django.dispatcher import DjangoWorkflowDispatcher
from ace_django.models import (
    InboxStatus,
    WorkflowEvent,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTransitionFailure,
)
from ace_django.store import DjangoExecutionStore
from ace_django.tests.helpers import FROZEN_NOW, FanInWorkflow

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def dispatch_setup():
    workflow = FanInWorkflow()
    registry = WorkflowRegistry()
    registry.register(workflow)
    store = DjangoExecutionStore()
    return (
        DjangoWorkflowClient(registry, store),
        workflow,
        DjangoWorkflowDispatcher(registry, store),
        DjangoWorkflowDispatcher(registry, store),
    )


def _start_runs(client: DjangoWorkflowClient, count: int = 1) -> list[WorkflowRun]:
    runs = [
        WorkflowRun.objects.get(pk=client.start("fan-in-test", {}, now=FROZEN_NOW).run_id)
        for _ in range(count)
    ]
    # Make the obstructed run sort first, independent of randomly assigned UUIDs.
    return sorted(runs, key=lambda run: run.pk)


def _signal(
    client: DjangoWorkflowClient, run: WorkflowRun, key: str
) -> WorkflowInboxEvent:
    receipt = client.signal(str(run.pk), key, idempotency_key=key, now=FROZEN_NOW)
    return WorkflowInboxEvent.objects.get(
        workflow_run=run, inbox_sequence=receipt.inbox_sequence
    )


@pytest.mark.parametrize("head_status", [InboxStatus.PENDING, InboxStatus.RETRYING])
def test_delayed_head_allows_other_run_to_progress_then_drains_in_order(
    dispatch_setup, head_status
) -> None:
    client, workflow, dispatcher, _ = dispatch_setup
    first_run, other_run = _start_runs(client, 2)
    head = _signal(client, first_run, "first")
    tail = _signal(client, first_run, "second")
    other = _signal(client, other_run, "ready")

    if head_status == InboxStatus.RETRYING:
        # Exercise the real failure writer and default jittered retry policy.
        with patch.object(workflow, "advance", side_effect=RuntimeError("retry later")):
            failure = dispatcher.dispatch_once(now=FROZEN_NOW)
        assert failure is not None and not failure.success
        assert failure.run_id == str(first_run.pk)
    else:
        # Named delayed-PENDING scenario: only this head gets a future deadline.
        WorkflowInboxEvent.objects.filter(pk=head.pk).update(
            available_at=FROZEN_NOW + timedelta(seconds=1)
        )
    head.refresh_from_db()
    assert head.status == head_status
    due_at = head.available_at
    assert due_at is not None and due_at > FROZEN_NOW

    result = dispatcher.dispatch_once(now=FROZEN_NOW)
    assert result is not None and result.success
    assert (result.run_id, result.inbox_sequence) == (str(other_run.pk), 1)
    other.refresh_from_db()
    assert other.status == InboxStatus.PROCESSED
    assert client.get_snapshot(str(other_run.pk)).state == {"completed": 1}
    assert WorkflowEvent.objects.filter(pk=other.pk).exists()

    for now in (FROZEN_NOW, due_at - timedelta(microseconds=1)):
        assert dispatcher.dispatch_once(now=now) is None
    head.refresh_from_db()
    tail.refresh_from_db()
    assert head.status == head_status
    assert tail.status == InboxStatus.PENDING
    assert tail.attempts == 0
    assert client.get_snapshot(str(first_run.pk)).state == {"completed": 0}
    assert not WorkflowEvent.objects.filter(pk__in=[head.pk, tail.pk]).exists()

    for inbox in (head, tail):
        result = dispatcher.dispatch_once(now=due_at)
        assert result is not None and result.success
        assert (result.run_id, result.inbox_sequence) == (
            str(first_run.pk), inbox.inbox_sequence
        )
        inbox.refresh_from_db()
        assert inbox.status == InboxStatus.PROCESSED
    snapshot = client.get_snapshot(str(first_run.pk))
    assert snapshot.status == WorkflowStatus.COMPLETED
    assert snapshot.state == {"completed": 2}
    assert list(
        WorkflowEvent.objects.filter(workflow_run=first_run, pk__in=[head.pk, tail.pk])
        .order_by("sequence")
        .values_list("pk", "sequence")
    ) == [(head.pk, 2), (tail.pk, 3)]
    assert dispatcher.dispatch_once(now=due_at) is None


def test_dead_letter_head_blocks_only_its_own_run(dispatch_setup) -> None:
    client, _, dispatcher, _ = dispatch_setup
    first_run, other_run = _start_runs(client, 2)
    head = _signal(client, first_run, "first")
    tail = _signal(client, first_run, "second")
    other = _signal(client, other_run, "ready")
    # Named recovery edge case: an active run still has an unresolved dead letter.
    # This must remain a barrier until operator recovery, not expose its tail.
    WorkflowInboxEvent.objects.filter(pk=head.pk).update(
        status=InboxStatus.DEAD_LETTER, dead_letter_at=FROZEN_NOW
    )

    assert dispatcher._get_next_inbox_sequence(first_run) == head.inbox_sequence
    result = dispatcher.dispatch_once(now=FROZEN_NOW)
    assert result is not None and result.success
    assert result.run_id == str(other_run.pk)
    other.refresh_from_db()
    assert other.status == InboxStatus.PROCESSED
    assert client.get_snapshot(str(other_run.pk)).state == {"completed": 1}
    assert dispatcher.dispatch_once(now=FROZEN_NOW) is None
    # A stale/out-of-order hint must obey the identical dead-letter barrier.
    rejected = dispatcher._dispatch_event(tail, FROZEN_NOW)
    assert not rejected.success
    head.refresh_from_db()
    tail.refresh_from_db()
    assert head.status == InboxStatus.DEAD_LETTER
    assert tail.status == InboxStatus.PENDING
    assert tail.attempts == 0
    assert not WorkflowTransitionFailure.objects.exists()
    assert not WorkflowEvent.objects.filter(pk__in=[head.pk, tail.pk]).exists()


def test_stale_candidate_cannot_bypass_concurrent_retry_backoff(dispatch_setup) -> None:
    client, workflow, dispatcher, competitor = dispatch_setup
    run = _start_runs(client)[0]
    head = _signal(client, run, "first")
    candidate = dispatcher._find_candidate(FROZEN_NOW)
    assert candidate is not None and candidate.pk == head.pk

    with patch.object(workflow, "advance", side_effect=RuntimeError("retry later")) as advance:
        winner = competitor.dispatch_once(now=FROZEN_NOW)
        assert winner is not None and not winner.success
        with patch.object(dispatcher, "_find_candidate", return_value=candidate):
            stale = dispatcher.dispatch_once(now=FROZEN_NOW)
        assert stale is not None and not stale.success
        assert stale.error == "Event is not yet available"
        assert advance.call_count == 1

    head.refresh_from_db()
    assert head.status == InboxStatus.RETRYING
    assert head.attempts == 1
    assert WorkflowTransitionFailure.objects.filter(inbox_event=head).count() == 1
    assert not WorkflowEvent.objects.filter(pk=head.pk).exists()
    assert client.get_snapshot(str(run.pk)).state == {"completed": 0}
    assert head.available_at is not None
    result = dispatcher.dispatch_once(now=head.available_at)
    assert result is not None and result.success
    head.refresh_from_db()
    assert head.status == InboxStatus.PROCESSED
    assert WorkflowEvent.objects.filter(pk=head.pk).count() == 1


@pytest.mark.parametrize("completes_run", [False, True])
def test_stale_candidate_cannot_reapply_or_discard_processed_event(
    dispatch_setup, completes_run
) -> None:
    client, workflow, dispatcher, competitor = dispatch_setup
    run = _start_runs(client)[0]
    if completes_run:
        _signal(client, run, "prelude")
        result = dispatcher.dispatch_once(now=FROZEN_NOW)
        assert result is not None and result.success
    head = _signal(client, run, "candidate")
    tail = _signal(client, run, "tail")
    candidate = dispatcher._find_candidate(FROZEN_NOW)
    assert candidate is not None and candidate.pk == head.pk

    with patch.object(workflow, "advance", wraps=workflow.advance) as advance:
        winner = competitor.dispatch_once(now=FROZEN_NOW)
        assert winner is not None and winner.success
        with patch.object(dispatcher, "_find_candidate", return_value=candidate):
            stale = dispatcher.dispatch_once(now=FROZEN_NOW + timedelta(seconds=1))
        assert stale is not None and not stale.success
        assert advance.call_count == 1

    head.refresh_from_db()
    tail.refresh_from_db()
    assert head.status == InboxStatus.PROCESSED
    assert head.processed_at == FROZEN_NOW
    assert head.discard_reason is None
    assert head.attempts == 0
    assert tail.status == (InboxStatus.DISCARDED if completes_run else InboxStatus.PENDING)
    assert WorkflowEvent.objects.filter(pk=head.pk).count() == 1
    assert not WorkflowTransitionFailure.objects.exists()
    snapshot = client.get_snapshot(str(run.pk))
    assert snapshot.state == {"completed": 2 if completes_run else 1}
    assert snapshot.status == (
        WorkflowStatus.COMPLETED if completes_run else WorkflowStatus.WAITING
    )


@pytest.mark.parametrize("status", [InboxStatus.DISCARDED, InboxStatus.DEAD_LETTER])
def test_stale_candidate_rechecks_inbox_status(dispatch_setup, status) -> None:
    client, workflow, dispatcher, _ = dispatch_setup
    run = _start_runs(client)[0]
    head = _signal(client, run, "candidate")
    candidate = dispatcher._find_candidate(FROZEN_NOW)
    assert candidate is not None
    # Simulate operator resolution after selection, before the dispatch locks.
    WorkflowInboxEvent.objects.filter(pk=head.pk).update(status=status)
    with patch.object(workflow, "advance", wraps=workflow.advance) as advance:
        result = dispatcher._dispatch_event(candidate, FROZEN_NOW)
        assert not result.success
        advance.assert_not_called()
    head.refresh_from_db()
    assert head.status == status
    assert head.attempts == 0
    assert not WorkflowTransitionFailure.objects.exists()
    assert not WorkflowEvent.objects.filter(pk=head.pk).exists()


@pytest.mark.parametrize("change", ["version", "blocked", "terminal"])
def test_stale_candidate_rechecks_run_eligibility(dispatch_setup, change) -> None:
    client, workflow, dispatcher, _ = dispatch_setup
    run = _start_runs(client)[0]
    head = _signal(client, run, "candidate")
    candidate = dispatcher._find_candidate(FROZEN_NOW)
    assert candidate is not None
    # Model a deployment/operator change between candidate selection and locking.
    updates = {
        "version": {"workflow_version": "unsupported"},
        "blocked": {"status": WorkflowStatus.BLOCKED.value},
        "terminal": {"status": WorkflowStatus.COMPLETED.value},
    }
    WorkflowRun.objects.filter(pk=run.pk).update(**updates[change])
    with patch.object(workflow, "advance", wraps=workflow.advance) as advance:
        result = dispatcher._dispatch_event(candidate, FROZEN_NOW)
        assert not result.success
        advance.assert_not_called()
    head.refresh_from_db()
    assert head.status == (InboxStatus.DISCARDED if change == "terminal" else InboxStatus.PENDING)
    assert head.attempts == 0
    assert not WorkflowTransitionFailure.objects.exists()
    assert not WorkflowEvent.objects.filter(pk=head.pk).exists()