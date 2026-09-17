from datetime import timedelta
from uuid import UUID

import pytest
from ace import WorkflowStatus
from ace_django.exceptions import LeaseOwnershipLost
from ace_django.models import (
    ActivityAttempt,
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkflowRun,
)
from ace_django.queue import DjangoActivityQueue
from ace_django.tests.helpers import (
    FROZEN_NOW,
    build_draining_engine,
    build_engine,
    build_group_engine,
    build_wait_all_group_engine,
)

pytestmark = pytest.mark.django_db

ACTIVITY_ID = "30000000-0000-0000-0000-000000000001"
FIRST_TOKEN = UUID("40000000-0000-0000-0000-000000000001")
SECOND_TOKEN = UUID("40000000-0000-0000-0000-000000000002")


class SequenceTokens:
    def __init__(self, *tokens: UUID) -> None:
        self._tokens = iter(tokens)

    def __call__(self) -> UUID:
        try:
            return next(self._tokens)
        except StopIteration as exc:
            raise AssertionError("ACE test token sequence was exhausted.") from exc


def _standalone_activity(*, max_attempts: int = 2) -> ActivityRun:
    return ActivityRun.objects.create(
        id=ACTIVITY_ID,
        activity_key="work",
        activity_name="tests.work",
        input={"subject": "risk-1"},
        retry_policy={
            "max_attempts": max_attempts,
            "initial_delay_seconds": 1,
            "backoff_multiplier": 2,
            "max_delay_seconds": 2,
        },
        available_at=FROZEN_NOW,
    )


def test_claim_renew_and_complete_standalone_activity() -> None:
    activity = _standalone_activity()
    queue = DjangoActivityQueue(token_factory=SequenceTokens(FIRST_TOKEN))

    lease = queue.claim("worker-1", now=FROZEN_NOW, lease_duration=timedelta(seconds=5))

    assert lease is not None
    assert lease.activity_run_id == str(activity.id)
    assert (
        queue.renew(
            lease,
            details={"phase": "working"},
            now=FROZEN_NOW + timedelta(seconds=1),
        )
        is True
    )
    assert queue.complete(lease, {"ok": True}, now=FROZEN_NOW + timedelta(seconds=2)) is None
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.SUCCEEDED
    assert activity.result == {"ok": True}
    assert activity.attempts.get().status == AttemptStatus.SUCCEEDED
    assert activity.attempts.get().details == {"phase": "working"}


def test_expired_lease_cannot_renew_or_complete_without_replacement() -> None:
    _standalone_activity()
    queue = DjangoActivityQueue(token_factory=SequenceTokens(FIRST_TOKEN))
    lease = queue.claim("worker-1", now=FROZEN_NOW, lease_duration=timedelta(seconds=1))
    assert lease is not None
    expired_at = FROZEN_NOW + timedelta(seconds=2)

    assert queue.renew(lease, now=expired_at) is False
    with pytest.raises(LeaseOwnershipLost, match="no longer owns"):
        queue.complete(lease, {"late": True}, now=expired_at)


def test_failure_retries_then_becomes_terminal() -> None:
    activity = _standalone_activity(max_attempts=2)
    queue = DjangoActivityQueue(token_factory=SequenceTokens(FIRST_TOKEN, SECOND_TOKEN))
    first = queue.claim("worker-1", now=FROZEN_NOW)
    assert first is not None

    queue.fail(first, error_type="Temporary", message="retry", now=FROZEN_NOW)
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.RETRYING
    assert activity.available_at == FROZEN_NOW + timedelta(seconds=1)
    assert queue.claim("worker-2", now=FROZEN_NOW) is None

    second = queue.claim("worker-2", now=FROZEN_NOW + timedelta(seconds=1))
    assert second is not None
    queue.fail(
        second,
        error_type="Permanent",
        message="failed",
        now=FROZEN_NOW + timedelta(seconds=1),
    )
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.FAILED
    assert activity.current_attempt == 2


def test_retry_success_clears_activity_failure_state() -> None:
    activity = _standalone_activity(max_attempts=2)
    queue = DjangoActivityQueue(token_factory=SequenceTokens(FIRST_TOKEN, SECOND_TOKEN))
    first = queue.claim("worker-1", now=FROZEN_NOW)
    assert first is not None
    queue.fail(first, error_type="Temporary", message="retry", now=FROZEN_NOW)

    second = queue.claim("worker-2", now=FROZEN_NOW + timedelta(seconds=1))
    assert second is not None
    queue.complete(second, {"ok": True}, now=FROZEN_NOW + timedelta(seconds=2))

    activity.refresh_from_db()
    assert activity.status == ActivityStatus.SUCCEEDED
    assert activity.result == {"ok": True}
    assert activity.failure is None
    assert activity.available_at is None
    assert list(activity.attempts.order_by("attempt_number").values_list("status", flat=True)) == [
        AttemptStatus.FAILED,
        AttemptStatus.SUCCEEDED,
    ]


def test_expired_lease_is_recovered_and_stale_completion_is_rejected() -> None:
    activity = _standalone_activity()
    queue = DjangoActivityQueue(token_factory=SequenceTokens(FIRST_TOKEN, SECOND_TOKEN))
    first = queue.claim("worker-1", now=FROZEN_NOW, lease_duration=timedelta(seconds=1))
    assert first is not None

    second = queue.claim(
        "worker-2",
        now=FROZEN_NOW + timedelta(seconds=2),
        lease_duration=timedelta(seconds=5),
    )

    assert second is not None
    assert second.attempt == 2
    with pytest.raises(LeaseOwnershipLost, match="no longer owns"):
        queue.complete(first, {"stale": True}, now=FROZEN_NOW + timedelta(seconds=2))
    queue.complete(second, {"ok": True}, now=FROZEN_NOW + timedelta(seconds=3))
    attempts = list(
        ActivityAttempt.objects.filter(activity_run=activity).order_by("attempt_number")
    )
    assert [attempt.status for attempt in attempts] == [
        AttemptStatus.EXPIRED,
        AttemptStatus.SUCCEEDED,
    ]


def test_workflow_activity_completion_advances_workflow() -> None:
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})
    activity = ActivityRun.objects.get(workflow_run_id=started.run_id)
    activity.available_at = FROZEN_NOW
    activity.save(update_fields=["available_at", "updated_at"])
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(FIRST_TOKEN))

    lease = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease is not None
    updated = queue.complete(lease, {"ok": True}, now=FROZEN_NOW)

    assert updated is not None
    assert updated.status == WorkflowStatus.WAITING
    run = WorkflowRun.objects.get(pk=started.run_id)
    assert run.last_event_sequence == 2
    assert run.timers.count() == 1


def test_late_completion_after_workflow_cancellation_terminalizes_activity() -> None:
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})
    activity = ActivityRun.objects.get(workflow_run_id=started.run_id)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(FIRST_TOKEN))
    lease = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease is not None

    cancelled = engine.request_cancellation(started.run_id, reason="operator request")
    result = queue.complete(lease, {"late": True}, now=FROZEN_NOW)

    activity.refresh_from_db()
    assert cancelled.status == WorkflowStatus.CANCELLED
    assert result is None
    assert activity.status == ActivityStatus.CANCELLED
    assert activity.attempts.get().status == AttemptStatus.CANCELLED


@pytest.mark.parametrize("outcome", ["complete", "fail", "cancel"])
def test_draining_cancellation_receives_terminal_activity_event(outcome: str) -> None:
    engine = build_draining_engine(
        "10000000-0000-0000-0000-000000000041",
        "20000000-0000-0000-0000-000000000041",
        "20000000-0000-0000-0000-000000000042",
        "20000000-0000-0000-0000-000000000043",
    )
    started = engine.start("draining-adapter-test", {})
    activity = ActivityRun.objects.get(workflow_run_id=started.run_id)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(FIRST_TOKEN))
    lease = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease is not None
    draining = engine.request_cancellation(started.run_id, reason="operator request")

    if outcome == "complete":
        queue.complete(lease, {"done": True}, now=FROZEN_NOW)
        expected_activity_status = ActivityStatus.SUCCEEDED
    elif outcome == "fail":
        queue.fail(lease, error_type="ExpectedFailure", message="failed", now=FROZEN_NOW)
        expected_activity_status = ActivityStatus.FAILED
    else:
        queue.cancel(lease, message="cooperative cancellation", now=FROZEN_NOW)
        expected_activity_status = ActivityStatus.CANCELLED

    activity.refresh_from_db()
    run = WorkflowRun.objects.get(pk=started.run_id)
    assert draining.status == WorkflowStatus.CANCELLING
    assert run.status == WorkflowStatus.CANCELLED
    assert activity.status == expected_activity_status


# ---------------------------------------------------------------------------
# Activity group queue tests — fan-in & fail-fast
# ---------------------------------------------------------------------------

# Engine IDs: run_id, start_event_id, group_completed/failed_event_id, workflow_complete/fail_event_id
GROUP_RUN_ID = "10000000-0000-0000-0000-000000000051"
GROUP_START_EID = "20000000-0000-0000-0000-000000000051"
GROUP_COMPLETE_EID = "20000000-0000-0000-0000-000000000052"
WF_COMPLETE_EID = "20000000-0000-0000-0000-000000000053"

TOKEN_A = UUID("40000000-0000-0000-0000-000000000011")
TOKEN_B = UUID("40000000-0000-0000-0000-000000000012")
TOKEN_A2 = UUID("40000000-0000-0000-0000-000000000013")
TOKEN_B2 = UUID("40000000-0000-0000-0000-000000000014")


def _start_group_workflow(
    *extra_event_ids: str,
) -> tuple[WorkflowRun, ActivityGroupRun, list[ActivityRun]]:
    """Start a group workflow and return (workflow, group, sorted members)."""
    all_ids = (GROUP_RUN_ID, GROUP_START_EID, *extra_event_ids)
    engine = build_group_engine(*all_ids)
    started = engine.start("group-adapter-test", {})
    wf = WorkflowRun.objects.get(pk=started.run_id)
    group = ActivityGroupRun.objects.get(workflow_run=wf)
    members = list(ActivityRun.objects.filter(group=group).order_by("activity_key"))
    # Make members claimable.
    for m in members:
        m.available_at = FROZEN_NOW
        m.save(update_fields=["available_at", "updated_at"])
    return wf, group, members


def test_group_first_member_success_no_event() -> None:
    """Completing the first of two members emits no workflow event."""
    _wf, group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A))

    lease = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease is not None
    result = queue.complete(lease, {"a": 1}, now=FROZEN_NOW)

    # No workflow event yet — only one of two members is done.
    assert result is None
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.RUNNING


def test_group_last_member_success_completes_group_and_workflow() -> None:
    """Both members succeeding triggers ACTIVITY_GROUP_COMPLETED → workflow completes."""
    wf, group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease_a is not None
    queue.complete(lease_a, {"a": 1}, now=FROZEN_NOW)

    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_b is not None
    snapshot = queue.complete(lease_b, {"b": 2}, now=FROZEN_NOW)

    # Group is SUCCEEDED with sorted result map.
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.SUCCEEDED
    assert group.result == {"extract-a": {"a": 1}, "extract-b": {"b": 2}}
    assert group.completed_at is not None

    # Workflow advanced to COMPLETED.
    assert snapshot is not None
    assert snapshot.status == WorkflowStatus.COMPLETED
    wf.refresh_from_db()
    assert wf.status == WorkflowStatus.COMPLETED


def test_group_fail_fast_cancels_siblings() -> None:
    """A member failing terminally triggers fail-fast: group FAILED, siblings cancelled."""
    wf, group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease_a is not None
    snapshot = queue.fail(
        lease_a,
        error_type="Permanent",
        message="extract-a failed",
        now=FROZEN_NOW,
    )

    # Group is FAILED.
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.FAILED
    assert group.failure is not None
    assert group.failure["failed_activity_key"] == "extract-a"
    assert group.completed_at is not None

    # Sibling (extract-b) was cancelled.
    sibling = ActivityRun.objects.get(activity_key="extract-b", group=group)
    assert sibling.status == ActivityStatus.CANCELLED
    assert sibling.completed_at is not None

    # Workflow advanced to FAILED.
    assert snapshot is not None
    assert snapshot.status == WorkflowStatus.FAILED
    wf.refresh_from_db()
    assert wf.status == WorkflowStatus.FAILED


def test_group_retry_no_event_retry_success_completes() -> None:
    """Member failure with retries remaining enters RETRYING — no group event.
    Retry success completes the group if sibling is also done."""
    _wf, group, members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    # extract-a needs max_attempts=2 to allow retry.
    extract_a = members[0]
    extract_a.retry_policy = {
        "max_attempts": 2,
        "initial_delay_seconds": 1,
        "backoff_multiplier": 1,
        "max_delay_seconds": 1,
    }
    extract_a.save(update_fields=["retry_policy", "updated_at"])

    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B, TOKEN_A2))

    # Claim and fail extract-a (attempt 1 of 2 → retries).
    lease_a1 = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease_a1 is not None
    result = queue.fail(
        lease_a1,
        error_type="Temporary",
        message="will retry",
        now=FROZEN_NOW,
    )
    assert result is None  # No group event — member is retrying.
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.RUNNING

    # Complete extract-b.
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_b is not None
    result = queue.complete(lease_b, {"b": 2}, now=FROZEN_NOW)
    assert result is None  # extract-a still retrying.

    # Retry extract-a succeeds.
    t1 = FROZEN_NOW + timedelta(seconds=1)
    lease_a2 = queue.claim("worker-1", now=t1)
    assert lease_a2 is not None
    assert lease_a2.attempt == 2
    snapshot = queue.complete(lease_a2, {"a": 1}, now=t1)

    # Group and workflow completed.
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.SUCCEEDED
    assert group.result == {"extract-a": {"a": 1}, "extract-b": {"b": 2}}
    assert snapshot is not None
    assert snapshot.status == WorkflowStatus.COMPLETED


def test_group_cancelled_members_not_claimable() -> None:
    """After fail-fast, cancelled siblings cannot be claimed."""
    _wf, _group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease_a is not None
    queue.fail(lease_a, error_type="Permanent", message="done", now=FROZEN_NOW)

    # extract-b should NOT be claimable (group is FAILED).
    assert queue.claim("worker-2", now=FROZEN_NOW) is None


def test_group_late_success_after_group_failed_records_outcome_silently() -> None:
    """If a member completes after the group already failed (in-flight race),
    the member outcome is recorded but no group/workflow event fires."""
    _wf, group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    # Claim both members.
    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_a is not None and lease_b is not None

    # Fail extract-a → group FAILED, extract-b still in-flight (RUNNING).
    queue.fail(lease_a, error_type="Permanent", message="fatal", now=FROZEN_NOW)
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.FAILED

    # extract-b was RUNNING when fail-fast happened, not READY/RETRYING,
    # so it was NOT cancelled. Late completion should be recorded silently.
    result = queue.complete(lease_b, {"b": 2}, now=FROZEN_NOW)
    assert result is None  # No event.

    extract_b = ActivityRun.objects.get(pk=lease_b.activity_run_id)
    assert extract_b.status == ActivityStatus.SUCCEEDED
    assert extract_b.result == {"b": 2}


def test_group_late_failure_after_group_failed_records_outcome_silently() -> None:
    """If a member fails after the group already failed, outcome recorded, no event."""
    _wf, _group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_a is not None and lease_b is not None

    # Fail extract-a.
    queue.fail(lease_a, error_type="Permanent", message="fatal", now=FROZEN_NOW)

    # Late failure of extract-b.
    result = queue.fail(
        lease_b,
        error_type="Permanent",
        message="also fatal",
        now=FROZEN_NOW,
    )
    assert result is None

    extract_b = ActivityRun.objects.get(pk=lease_b.activity_run_id)
    assert extract_b.status == ActivityStatus.FAILED


def test_group_completion_in_terminal_workflow_records_group_without_event() -> None:
    """If the workflow reached a terminal state while members were in flight,
    the last member's completion records the group outcome but emits nothing."""
    wf, group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_a is not None and lease_b is not None
    queue.complete(lease_a, {"a": 1}, now=FROZEN_NOW)

    # Workflow becomes terminal out-of-band (e.g. a standalone failure path).
    WorkflowRun.objects.filter(pk=wf.pk).update(status=WorkflowStatus.FAILED)

    # Previously raised InvalidTransition and rolled back the member's success.
    result = queue.complete(lease_b, {"b": 2}, now=FROZEN_NOW)
    assert result is None

    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.SUCCEEDED
    extract_b = ActivityRun.objects.get(pk=lease_b.activity_run_id)
    assert extract_b.status == ActivityStatus.SUCCEEDED
    assert extract_b.result == {"b": 2}


def test_group_failure_in_terminal_workflow_records_group_without_event() -> None:
    """Fail-fast in a terminal workflow marks the group FAILED, cancels
    unclaimed siblings, and emits nothing."""
    wf, group, _members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease_a is not None

    WorkflowRun.objects.filter(pk=wf.pk).update(status=WorkflowStatus.FAILED)

    result = queue.fail(lease_a, error_type="Permanent", message="fatal", now=FROZEN_NOW)
    assert result is None

    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.FAILED
    sibling = ActivityRun.objects.get(activity_key="extract-b", group=group)
    assert sibling.status == ActivityStatus.CANCELLED


def test_group_member_of_terminal_group_does_not_retry() -> None:
    """An in-flight member of an already-failed group fails terminally instead
    of entering RETRYING (which would be an unclaimable zombie row)."""
    _wf, group, members = _start_group_workflow(GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    # extract-b gets retries so the retry branch would otherwise be taken.
    extract_b = members[1]
    extract_b.retry_policy = {
        "max_attempts": 3,
        "initial_delay_seconds": 1,
        "backoff_multiplier": 1,
        "max_delay_seconds": 1,
    }
    extract_b.save(update_fields=["retry_policy", "updated_at"])

    engine = build_group_engine(GROUP_RUN_ID, GROUP_START_EID, GROUP_COMPLETE_EID, WF_COMPLETE_EID)
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_a is not None and lease_b is not None

    # extract-a fails terminally → group FAILED while extract-b is in flight.
    queue.fail(lease_a, error_type="Permanent", message="fatal", now=FROZEN_NOW)
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.FAILED

    # extract-b fails with retries remaining — must NOT enter RETRYING.
    result = queue.fail(lease_b, error_type="Temporary", message="would retry", now=FROZEN_NOW)
    assert result is None

    extract_b.refresh_from_db()
    assert extract_b.status == ActivityStatus.FAILED
    assert extract_b.available_at is None
    assert queue.claim("worker-3", now=FROZEN_NOW + timedelta(seconds=5)) is None


# ---------------------------------------------------------------------------
# Activity group queue tests — WAIT_ALL (settle-all, continue-on-failure)
# ---------------------------------------------------------------------------

WAIT_ALL_RUN_ID = "10000000-0000-0000-0000-000000000061"
WAIT_ALL_START_EID = "20000000-0000-0000-0000-000000000061"
WAIT_ALL_SETTLE_EID = "20000000-0000-0000-0000-000000000062"
WAIT_ALL_WF_COMPLETE_EID = "20000000-0000-0000-0000-000000000063"


def _start_wait_all_group_workflow(
    *extra_event_ids: str,
) -> tuple[WorkflowRun, ActivityGroupRun, list[ActivityRun]]:
    all_ids = (WAIT_ALL_RUN_ID, WAIT_ALL_START_EID, *extra_event_ids)
    engine = build_wait_all_group_engine(*all_ids)
    started = engine.start("wait-all-group-test", {})
    wf = WorkflowRun.objects.get(pk=started.run_id)
    group = ActivityGroupRun.objects.get(workflow_run=wf)
    members = list(ActivityRun.objects.filter(group=group).order_by("activity_key"))
    for m in members:
        m.available_at = FROZEN_NOW
        m.save(update_fields=["available_at", "updated_at"])
    return wf, group, members


def test_wait_all_group_member_failure_does_not_cancel_sibling() -> None:
    """Unlike ALL_SUCCESS, a permanent member failure under WAIT_ALL leaves
    the group RUNNING and the sibling claimable — no fail-fast cancellation."""
    _wf, group, _members = _start_wait_all_group_workflow(
        WAIT_ALL_SETTLE_EID, WAIT_ALL_WF_COMPLETE_EID
    )
    engine = build_wait_all_group_engine(
        WAIT_ALL_RUN_ID, WAIT_ALL_START_EID, WAIT_ALL_SETTLE_EID, WAIT_ALL_WF_COMPLETE_EID
    )
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    assert lease_a is not None
    result = queue.fail(lease_a, error_type="Permanent", message="extract-a failed", now=FROZEN_NOW)

    assert result is None  # Sibling still outstanding — group not settled yet.
    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.RUNNING

    sibling = ActivityRun.objects.get(activity_key="extract-b", group=group)
    assert sibling.status == ActivityStatus.READY  # NOT cancelled.


def test_wait_all_group_settles_with_partial_failure_and_completes_workflow() -> None:
    """Once every member is terminal, WAIT_ALL emits ACTIVITY_GROUP_COMPLETED
    (never GROUP_FAILED) with both `results` and `failures` — the workflow
    decides to continue past the partial failure."""
    wf, group, _members = _start_wait_all_group_workflow(
        WAIT_ALL_SETTLE_EID, WAIT_ALL_WF_COMPLETE_EID
    )
    engine = build_wait_all_group_engine(
        WAIT_ALL_RUN_ID, WAIT_ALL_START_EID, WAIT_ALL_SETTLE_EID, WAIT_ALL_WF_COMPLETE_EID
    )
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_a is not None and lease_b is not None

    queue.fail(lease_a, error_type="Permanent", message="extract-a failed", now=FROZEN_NOW)
    snapshot = queue.complete(lease_b, {"b": 2}, now=FROZEN_NOW)

    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.FAILED
    assert group.result == {"extract-b": {"b": 2}}
    assert group.failure == {
        "failures": {
            "extract-a": {
                "error_type": "Permanent",
                "message": "extract-a failed",
                "details": {},
            }
        }
    }

    # Workflow saw a single ACTIVITY_GROUP_COMPLETED and completed itself.
    assert snapshot is not None
    assert snapshot.status == WorkflowStatus.COMPLETED
    assert snapshot.result == {
        "results": {"extract-b": {"b": 2}},
        "failures": {
            "extract-a": {
                "error_type": "Permanent",
                "message": "extract-a failed",
                "details": {},
            }
        },
    }
    wf.refresh_from_db()
    assert wf.status == WorkflowStatus.COMPLETED


def test_wait_all_group_all_success_settles_succeeded() -> None:
    """WAIT_ALL with no failures behaves like ALL_SUCCESS: group SUCCEEDED,
    empty `failures` map, workflow completes."""
    wf, group, _members = _start_wait_all_group_workflow(
        WAIT_ALL_SETTLE_EID, WAIT_ALL_WF_COMPLETE_EID
    )
    engine = build_wait_all_group_engine(
        WAIT_ALL_RUN_ID, WAIT_ALL_START_EID, WAIT_ALL_SETTLE_EID, WAIT_ALL_WF_COMPLETE_EID
    )
    queue = DjangoActivityQueue(engine, token_factory=SequenceTokens(TOKEN_A, TOKEN_B))

    lease_a = queue.claim("worker-1", now=FROZEN_NOW)
    lease_b = queue.claim("worker-2", now=FROZEN_NOW)
    assert lease_a is not None and lease_b is not None
    queue.complete(lease_a, {"a": 1}, now=FROZEN_NOW)
    snapshot = queue.complete(lease_b, {"b": 2}, now=FROZEN_NOW)

    group.refresh_from_db()
    assert group.status == ActivityGroupStatus.SUCCEEDED
    assert group.result == {"extract-a": {"a": 1}, "extract-b": {"b": 2}}
    assert group.failure is None

    assert snapshot is not None
    assert snapshot.status == WorkflowStatus.COMPLETED
    wf.refresh_from_db()
    assert wf.status == WorkflowStatus.COMPLETED
