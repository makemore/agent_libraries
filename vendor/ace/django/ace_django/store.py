"""Transactional Django implementation of the ACE execution store."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from ace import (
    CancelWorkflow,
    CompleteWorkflow,
    ConcurrentTransition,
    FailWorkflow,
    IdempotencyConflict,
    InvalidTransition,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowFailure,
    WorkflowSnapshot,
    WorkflowStatus,
)
from django.db import IntegrityError, transaction

from ace_django.exceptions import InvalidIdentifier
from ace_django.models import (
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    TimerStatus,
    WorkflowEvent,
    WorkflowRun,
    WorkflowTimer,
)

if TYPE_CHECKING:
    from ace.commands import WorkflowCommand
    from ace.models import WorkflowEvent as CoreWorkflowEvent


class DjangoExecutionStore:
    def find_idempotent(
        self,
        namespace: str,
        workflow_name: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot | None:
        run = WorkflowRun.objects.filter(
            namespace=namespace,
            workflow_name=workflow_name,
            idempotency_key=idempotency_key,
        ).first()
        return _to_snapshot(run) if run is not None else None

    def start(
        self,
        snapshot: WorkflowSnapshot,
        event: CoreWorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
        *,
        idempotency_key: str | None,
    ) -> WorkflowSnapshot:
        try:
            with transaction.atomic():
                if idempotency_key is not None:
                    existing = (
                        WorkflowRun.objects.select_for_update()
                        .filter(
                            namespace=snapshot.namespace,
                            workflow_name=snapshot.workflow_name,
                            idempotency_key=idempotency_key,
                        )
                        .first()
                    )
                    if existing is not None:
                        _ensure_same_start(existing, snapshot, idempotency_key)
                        return _to_snapshot(existing)

                run = WorkflowRun.objects.create(
                    id=_uuid(snapshot.run_id, "workflow run"),
                    namespace=snapshot.namespace,
                    workflow_name=snapshot.workflow_name,
                    workflow_version=snapshot.workflow_version,
                    status=snapshot.status.value,
                    input=snapshot.input,
                    state=snapshot.state,
                    result=snapshot.result,
                    failure=_failure_dict(snapshot.failure),
                    idempotency_key=idempotency_key,
                    last_event_sequence=snapshot.last_event_sequence,
                    started_at=snapshot.created_at or event.occurred_at,
                    completed_at=(snapshot.updated_at if snapshot.status.is_terminal else None),
                )
                _append_event(run, event)
                _materialize_commands(run, commands, event.occurred_at, now=event.occurred_at)
                return _to_snapshot(run)
        except IntegrityError:
            if idempotency_key is None:
                raise
            existing = WorkflowRun.objects.filter(
                namespace=snapshot.namespace,
                workflow_name=snapshot.workflow_name,
                idempotency_key=idempotency_key,
            ).first()
            if existing is None:
                raise
            _ensure_same_start(existing, snapshot, idempotency_key)
            return _to_snapshot(existing)

    def load(self, run_id: str) -> WorkflowSnapshot:
        run = WorkflowRun.objects.get(pk=_uuid(run_id, "workflow run"))
        return _to_snapshot(run)

    def commit(
        self,
        expected_sequence: int,
        snapshot: WorkflowSnapshot,
        event: CoreWorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
    ) -> WorkflowSnapshot:
        with transaction.atomic():
            run = WorkflowRun.objects.select_for_update().get(
                pk=_uuid(snapshot.run_id, "workflow run")
            )
            if run.last_event_sequence != expected_sequence:
                raise ConcurrentTransition(
                    f"Workflow run {snapshot.run_id!r} expected sequence {expected_sequence}, "
                    f"but the current sequence is {run.last_event_sequence}."
                )

            run.status = snapshot.status.value
            run.state = snapshot.state
            run.result = snapshot.result
            run.failure = _failure_dict(snapshot.failure)
            run.last_event_sequence = snapshot.last_event_sequence
            if event.event_type == "workflow.cancellation_requested":
                if snapshot.status == WorkflowStatus.CANCELLING and (
                    ActivityGroupRun.objects.filter(
                        workflow_run=run,
                        status=ActivityGroupStatus.RUNNING,
                    ).exists()
                ):
                    # A draining workflow never receives events for its group
                    # members (groups emit no per-member events and a cancelled
                    # group emits nothing), so it would hang in CANCELLING
                    # forever. Reject loudly instead.
                    raise InvalidTransition(
                        f"Workflow run {snapshot.run_id!r} cannot drain (CANCELLING) "
                        "while an activity group is in flight. Respond to "
                        "cancellation with CancelWorkflow instead."
                    )
                run.cancel_requested_at = event.occurred_at
                _cancel_unclaimed_work(run, event.occurred_at)
            if snapshot.status.is_terminal:
                run.completed_at = snapshot.updated_at or event.occurred_at
            run.save(
                update_fields=[
                    "status",
                    "state",
                    "result",
                    "failure",
                    "last_event_sequence",
                    "cancel_requested_at",
                    "completed_at",
                    "updated_at",
                ]
            )
            _append_event(run, event)
            _materialize_commands(run, commands, event.occurred_at, now=event.occurred_at)
            return _to_snapshot(run)


def _uuid(value: str, label: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as exc:
        raise InvalidIdentifier(f"ACE {label} ID {value!r} must be a UUID string.") from exc


def _failure_dict(failure: WorkflowFailure | None) -> dict | None:
    return asdict(failure) if failure is not None else None


def _to_snapshot(run: WorkflowRun) -> WorkflowSnapshot:
    failure = WorkflowFailure(**run.failure) if run.failure is not None else None
    return WorkflowSnapshot(
        run_id=str(run.id),
        namespace=run.namespace,
        workflow_name=run.workflow_name,
        workflow_version=run.workflow_version,
        status=WorkflowStatus(run.status),
        input=run.input,
        state=run.state,
        result=run.result,
        failure=failure,
        last_event_sequence=run.last_event_sequence,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _ensure_same_start(
    existing: WorkflowRun,
    snapshot: WorkflowSnapshot,
    idempotency_key: str,
) -> None:
    if existing.workflow_version != snapshot.workflow_version or existing.input != snapshot.input:
        raise IdempotencyConflict(
            f"Workflow idempotency key {idempotency_key!r} in namespace "
            f"{snapshot.namespace!r} was reused with different input or version."
        )


def _append_event(run: WorkflowRun, event: CoreWorkflowEvent) -> None:
    WorkflowEvent.objects.create(
        id=_uuid(event.event_id, "workflow event"),
        workflow_run=run,
        sequence=event.sequence,
        event_type=event.event_type,
        payload=event.payload,
        actor=event.actor,
        occurred_at=event.occurred_at,
    )


def _materialize_commands(
    run: WorkflowRun,
    commands: tuple[WorkflowCommand, ...],
    available_at,
    now: datetime | None = None,
) -> None:
    for command in commands:
        if isinstance(command, ScheduleActivity):
            _materialize_activity(run, command, available_at, group=None, now=now)
        elif isinstance(command, ScheduleActivityGroup):
            group = ActivityGroupRun.objects.create(
                workflow_run=run,
                group_key=command.group_key,
                completion_policy=command.completion_policy.value,
                status=ActivityGroupStatus.RUNNING,
            )
            for member in command.activities:
                _materialize_activity(run, member, available_at, group=group, now=now)
        elif isinstance(command, ScheduleTimer):
            WorkflowTimer.objects.create(
                workflow_run=run,
                timer_key=command.timer_key,
                fire_at=command.fire_at,
                payload=command.payload,
            )
        elif isinstance(command, (CompleteWorkflow, FailWorkflow, CancelWorkflow)):
            continue


def _materialize_activity(
    run: WorkflowRun,
    command: ScheduleActivity,
    available_at,
    *,
    group: ActivityGroupRun | None,
    now: datetime | None = None,
) -> ActivityRun:
    from ace_django.models import ActivityExecutionMode

    # Extract timeout config
    timeout = command.timeout if hasattr(command, "timeout") else None

    # Calculate schedule_to_close_at if schedule_to_close_seconds is set
    schedule_to_close_at = None
    schedule_to_close_seconds = timeout.schedule_to_close_seconds if timeout else None
    if schedule_to_close_seconds is not None and now is not None:
        schedule_to_close_at = now + timedelta(seconds=schedule_to_close_seconds)

    # Map execution_mode from command
    execution_mode = ActivityExecutionMode.STANDARD
    if hasattr(command, "execution_mode") and command.execution_mode is not None:
        execution_mode = (
            command.execution_mode.value
            if hasattr(command.execution_mode, "value")
            else str(command.execution_mode)
        )

    return ActivityRun.objects.create(
        workflow_run=run,
        group=group,
        namespace=run.namespace,
        activity_key=command.activity_key,
        activity_name=command.activity_name,
        activity_version=command.activity_version,
        queue=command.queue,
        priority=getattr(command, "priority", 0),
        input=command.input,
        retry_policy=asdict(command.retry_policy),
        idempotency_key=command.idempotency_key,
        available_at=available_at,
        execution_mode=execution_mode,
        partition_key=getattr(command, "partition_key", None),
        schedule_to_close_seconds=schedule_to_close_seconds,
        start_to_close_seconds=timeout.start_to_close_seconds if timeout else None,
        heartbeat_seconds=timeout.heartbeat_seconds if timeout else None,
        schedule_to_close_at=schedule_to_close_at,
    )


def _cancel_unclaimed_work(run: WorkflowRun, cancelled_at) -> None:
    # Cancel unclaimed standalone and grouped activities.
    ActivityRun.objects.filter(
        workflow_run=run,
        status__in=(ActivityStatus.READY, ActivityStatus.RETRYING),
    ).update(
        status=ActivityStatus.CANCELLED,
        completed_at=cancelled_at,
        available_at=None,
        updated_at=cancelled_at,
    )
    # Set every nonterminal group to CANCELLED.
    ActivityGroupRun.objects.filter(
        workflow_run=run,
        status=ActivityGroupStatus.RUNNING,
    ).update(
        status=ActivityGroupStatus.CANCELLED,
        completed_at=cancelled_at,
        updated_at=cancelled_at,
    )
    WorkflowTimer.objects.filter(
        workflow_run=run,
        status=TimerStatus.SCHEDULED,
    ).update(
        status=TimerStatus.CANCELLED,
        cancelled_at=cancelled_at,
        updated_at=cancelled_at,
    )
