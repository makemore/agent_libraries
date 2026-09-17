"""Pure workflow transition engine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from ace.commands import (
    ActivityGroupCompletionPolicy,
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowCommand,
    WorkflowTransition,
)
from ace.exceptions import IdempotencyConflict, InvalidClock, InvalidTransition
from ace.models import (
    WorkflowContext,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowFailure,
    WorkflowSnapshot,
    WorkflowStatus,
)

if TYPE_CHECKING:
    from datetime import datetime

    from ace.definitions import Clock, IdGenerator
    from ace.json_types import JsonObject, JsonValue
    from ace.registry import WorkflowRegistry
    from ace.store import ExecutionStore


class WorkflowEngine:
    def __init__(
        self,
        registry: WorkflowRegistry,
        store: ExecutionStore,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock
        self._ids = ids

    def start(
        self,
        workflow_name: str,
        input: JsonObject,
        *,
        version: str = "1",
        namespace: str = "default",
        idempotency_key: str | None = None,
        actor: str | None = None,
    ) -> WorkflowSnapshot:
        if idempotency_key is not None and not idempotency_key.strip():
            raise InvalidTransition("Workflow idempotency_key cannot be blank.")
        if idempotency_key is not None:
            existing = self._store.find_idempotent(
                namespace,
                workflow_name,
                idempotency_key,
            )
            if existing is not None:
                if existing.workflow_version != version or existing.input != input:
                    raise IdempotencyConflict(
                        f"Workflow idempotency key {idempotency_key!r} in namespace "
                        f"{namespace!r} was reused with different input or version."
                    )
                return existing
        definition = self._registry.resolve(workflow_name, version)
        now = self._now()
        run_id = self._ids.new_id()
        event = WorkflowEvent(
            event_id=self._ids.new_id(),
            run_id=run_id,
            sequence=1,
            event_type=WorkflowEventType.WORKFLOW_STARTED,
            occurred_at=now,
            payload={"workflow_name": workflow_name, "workflow_version": version},
            actor=actor,
        )
        transition = definition.start(
            deepcopy(input),
            WorkflowContext(run_id=run_id, now=now),
        )
        status, result, failure = _validate_transition(transition)
        snapshot = WorkflowSnapshot(
            run_id=run_id,
            namespace=namespace,
            workflow_name=workflow_name,
            workflow_version=version,
            status=status,
            input=deepcopy(input),
            state=deepcopy(transition.state),
            result=deepcopy(result),
            failure=deepcopy(failure),
            last_event_sequence=1,
            created_at=now,
            updated_at=now,
        )
        return self._store.start(
            snapshot,
            event,
            transition.commands,
            idempotency_key=idempotency_key,
        )

    def handle_event(
        self,
        run_id: str,
        event_type: str,
        *,
        payload: JsonObject | None = None,
        actor: str | None = None,
    ) -> WorkflowSnapshot:
        current = self._store.load(run_id)
        if current.status.is_terminal:
            raise InvalidTransition(
                f"Workflow run {run_id!r} is already terminal with status {current.status}."
            )
        definition = self._registry.resolve(current.workflow_name, current.workflow_version)
        now = self._now()
        event = WorkflowEvent(
            event_id=self._ids.new_id(),
            run_id=run_id,
            sequence=current.last_event_sequence + 1,
            event_type=event_type,
            occurred_at=now,
            payload=deepcopy(payload or {}),
            actor=actor,
        )
        transition = definition.advance(
            deepcopy(current.state),
            deepcopy(event),
            WorkflowContext(run_id=run_id, now=now),
        )
        status, result, failure = _validate_transition(transition)
        updated = replace(
            current,
            status=status,
            state=deepcopy(transition.state),
            result=deepcopy(result),
            failure=deepcopy(failure),
            last_event_sequence=event.sequence,
            updated_at=now,
        )
        return self._store.commit(
            current.last_event_sequence,
            updated,
            event,
            transition.commands,
        )

    def request_cancellation(
        self,
        run_id: str,
        *,
        reason: str,
        actor: str | None = None,
    ) -> WorkflowSnapshot:
        return self.handle_event(
            run_id,
            WorkflowEventType.CANCELLATION_REQUESTED,
            payload={"reason": reason},
            actor=actor,
        )

    def apply_event(
        self,
        current: WorkflowSnapshot,
        event: WorkflowEvent,
    ) -> tuple[WorkflowSnapshot, tuple[WorkflowCommand, ...]]:
        """Apply a pre-built event to a workflow snapshot.

        This is the dispatcher-facing API: the event ID and timestamp are
        supplied by the persistence layer rather than generated here. Returns
        the updated snapshot and emitted commands for atomic persistence.

        For workflow.deadline_exceeded events, the engine applies a built-in
        terminal transition (FAILED) without calling the workflow definition.
        """
        if current.status.is_terminal:
            raise InvalidTransition(
                f"Workflow run {current.run_id!r} is already terminal with status {current.status}."
            )
        if event.sequence != current.last_event_sequence + 1:
            raise InvalidTransition(
                f"Event sequence {event.sequence} does not follow "
                f"last_event_sequence {current.last_event_sequence}."
            )

        # Built-in deadline handling: no workflow definition call needed.
        if event.event_type == WorkflowEventType.WORKFLOW_DEADLINE_EXCEEDED:
            failure = WorkflowFailure(
                error_type="WorkflowDeadlineExceeded",
                message=event.payload.get("message", "Workflow deadline exceeded."),
                details={"deadline_at": event.payload.get("deadline_at")},
            )
            updated = replace(
                current,
                status=WorkflowStatus.FAILED,
                failure=failure,
                last_event_sequence=event.sequence,
                updated_at=event.occurred_at,
            )
            commands: tuple[WorkflowCommand, ...] = (FailWorkflow(failure=failure),)
            return updated, commands

        definition = self._registry.resolve(current.workflow_name, current.workflow_version)
        transition = definition.advance(
            deepcopy(current.state),
            deepcopy(event),
            WorkflowContext(run_id=current.run_id, now=event.occurred_at),
        )
        status, result, failure = _validate_transition(transition)
        updated = replace(
            current,
            status=status,
            state=deepcopy(transition.state),
            result=deepcopy(result),
            failure=deepcopy(failure),
            last_event_sequence=event.sequence,
            updated_at=event.occurred_at,
        )
        return updated, transition.commands

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidClock("ACE clocks must return timezone-aware timestamps.")
        return now


def _validate_transition(
    transition: WorkflowTransition,
) -> tuple[WorkflowStatus, JsonValue, WorkflowFailure | None]:
    # Collect all activity keys (standalone + group members) for cross-collision checks.
    all_activity_keys: list[str] = []
    group_keys: list[str] = []

    for command in transition.commands:
        if isinstance(command, ScheduleActivity):
            _validate_activity(command)
            all_activity_keys.append(command.activity_key)
        elif isinstance(command, ScheduleActivityGroup):
            _validate_group(command)
            group_keys.append(command.group_key)
            for member in command.activities:
                all_activity_keys.append(member.activity_key)
        elif isinstance(command, ScheduleTimer):
            _require_nonblank("Timer key", command.timer_key)
            if command.fire_at.tzinfo is None or command.fire_at.utcoffset() is None:
                raise InvalidTransition("Timer fire_at must be timezone-aware.")

    terminal = tuple(
        command
        for command in transition.commands
        if isinstance(command, (CompleteWorkflow, FailWorkflow, CancelWorkflow))
    )
    if len(terminal) > 1:
        raise InvalidTransition("A workflow transition can contain only one terminal command.")
    if terminal and len(transition.commands) > 1:
        raise InvalidTransition("A terminal command cannot be combined with other commands.")

    # Cross-collision: all activity keys unique across standalone + groups.
    if len(all_activity_keys) != len(set(all_activity_keys)):
        raise InvalidTransition("Activity keys must be unique within one transition.")
    if len(group_keys) != len(set(group_keys)):
        raise InvalidTransition("Group keys must be unique within one transition.")

    timer_keys = [
        command.timer_key for command in transition.commands if isinstance(command, ScheduleTimer)
    ]
    if len(timer_keys) != len(set(timer_keys)):
        raise InvalidTransition("Timer keys must be unique within one transition.")

    if terminal:
        if transition.status is not None:
            raise InvalidTransition("Terminal status is derived from the terminal command.")
        command = terminal[0]
        if isinstance(command, CompleteWorkflow):
            return WorkflowStatus.COMPLETED, command.result, None
        if isinstance(command, FailWorkflow):
            return WorkflowStatus.FAILED, None, command.failure
        return WorkflowStatus.CANCELLED, {"reason": command.reason}, None

    allowed = {WorkflowStatus.RUNNING, WorkflowStatus.WAITING, WorkflowStatus.CANCELLING}
    if transition.status is not None and transition.status not in allowed:
        raise InvalidTransition(
            f"Non-terminal transition cannot set workflow status {transition.status}."
        )
    if transition.status is not None:
        return transition.status, None, None
    has_work = any(
        isinstance(command, (ScheduleActivity, ScheduleActivityGroup))
        for command in transition.commands
    )
    return (WorkflowStatus.RUNNING if has_work else WorkflowStatus.WAITING), None, None


def _validate_activity(activity: ScheduleActivity) -> None:
    """Validate a single ScheduleActivity command."""
    _require_nonblank("Activity key", activity.activity_key)
    _require_nonblank("Activity name", activity.activity_name)
    _require_nonblank("Activity version", activity.activity_version)
    _require_nonblank("Activity queue", activity.queue)
    if activity.idempotency_key is not None:
        _require_nonblank("Activity idempotency key", activity.idempotency_key)


def _validate_group(group: ScheduleActivityGroup) -> None:
    """Validate a ScheduleActivityGroup command."""
    _require_nonblank("Group key", group.group_key)
    if not group.activities:
        raise InvalidTransition(
            f"Activity group {group.group_key!r} must contain at least one activity."
        )
    if group.completion_policy not in ActivityGroupCompletionPolicy:
        raise InvalidTransition(
            f"Activity group {group.group_key!r} has unsupported completion policy "
            f"{group.completion_policy!r}."
        )
    member_keys: list[str] = []
    for member in group.activities:
        _validate_activity(member)
        member_keys.append(member.activity_key)
    if len(member_keys) != len(set(member_keys)):
        raise InvalidTransition(f"Activity keys within group {group.group_key!r} must be unique.")


def _require_nonblank(label: str, value: str) -> None:
    if not value.strip():
        raise InvalidTransition(f"{label} cannot be blank.")
