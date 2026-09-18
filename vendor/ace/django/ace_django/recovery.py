"""Explicit fenced recovery; callers own authorization and recovery budgets."""

from ace import WorkflowStatus
from django.db import transaction
from django.utils import timezone

from .models import InboxStatus, WorkflowInboxEvent, WorkflowRun


@transaction.atomic(using="default")
def resume_workflow(*, run_id, expected_blocked_at):
    """Retry unconsumed inbox transitions, never replace activities or agent runs.

    The exact blocking timestamp prevents a delayed command resuming a different
    failure. Prior failure records remain intact. No diagnostic text is returned.
    """
    run = WorkflowRun.objects.using("default").select_for_update().get(pk=run_id)
    allowed = {WorkflowStatus.PENDING.value, WorkflowStatus.RUNNING.value, WorkflowStatus.WAITING.value}
    if (run.status != WorkflowStatus.BLOCKED.value or run.blocked_at is None
            or run.blocked_at != expected_blocked_at or run.blocked_from_status not in allowed
            or run.cancel_requested_at or run.completed_at):
        raise ValueError("Workflow recovery subject is unavailable or changed.")
    now = timezone.now()
    count = WorkflowInboxEvent.objects.using("default").filter(
        workflow_run=run, status=InboxStatus.DEAD_LETTER, processed_at__isnull=True,
        inbox_sequence__gt=run.last_inbox_sequence,
    ).update(status=InboxStatus.RETRYING, attempts=0, available_at=now, updated_at=now)
    run.status = run.blocked_from_status
    run.blocked_at = run.blocked_from_status = run.block_reason = None
    run.save(using="default", update_fields=(
        "status", "blocked_at", "blocked_from_status", "block_reason", "updated_at"))
    return count