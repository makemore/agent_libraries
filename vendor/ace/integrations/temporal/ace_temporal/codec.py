"""Translation of ACE workflow commands into Temporal-native actions.

Commands are validated against declared backend capabilities before
translation. Unsupported semantics raise UnsupportedCommand instead of being
weakly emulated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ace.commands import (
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
)
from ace.exceptions import AceError

if TYPE_CHECKING:
    from datetime import datetime

    from ace.backend import BackendCapabilities
    from ace.commands import WorkflowCommand
    from ace.json_types import JsonObject, JsonValue
    from ace.models import RetryPolicy, WorkflowFailure


class UnsupportedCommand(AceError):
    """A command requires semantics the backend does not declare."""


class TemporalActionKind(StrEnum):
    """Backend-native action a translated command maps to."""

    # workflow.execute_activity with retry policy and timeouts
    EXECUTE_ACTIVITY = "execute_activity"
    # workflow.sleep / timer until fire_at
    START_TIMER = "start_timer"
    COMPLETE_WORKFLOW = "complete_workflow"
    FAIL_WORKFLOW = "fail_workflow"
    CANCEL_WORKFLOW = "cancel_workflow"


@dataclass(frozen=True)
class TemporalCommandPlan:
    """Backend-native description of one translated workflow command."""

    kind: TemporalActionKind
    activity_key: str | None = None
    activity_name: str | None = None
    activity_version: str | None = None
    input: JsonObject | None = None
    retry_policy: RetryPolicy | None = None
    schedule_to_close_seconds: float | None = None
    start_to_close_seconds: float | None = None
    heartbeat_seconds: float | None = None
    timer_key: str | None = None
    fire_at: datetime | None = None
    result: JsonValue = None
    failure: WorkflowFailure | None = None
    reason: str | None = None


def translate_command(
    command: WorkflowCommand,
    capabilities: BackendCapabilities,
) -> TemporalCommandPlan:
    """Translate one command, raising UnsupportedCommand on capability gaps."""
    if isinstance(command, ScheduleActivity):
        return _translate_activity(command, capabilities)
    if isinstance(command, ScheduleActivityGroup):
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} declares no activity-group support; "
            f"group {command.group_key!r} cannot be scheduled."
        )
    if isinstance(command, ScheduleTimer):
        if not capabilities.supports_timers:
            raise UnsupportedCommand(f"Backend {capabilities.name!r} does not support timers.")
        return TemporalCommandPlan(
            kind=TemporalActionKind.START_TIMER,
            timer_key=command.timer_key,
            fire_at=command.fire_at,
            input=command.payload,
        )
    if isinstance(command, CompleteWorkflow):
        return TemporalCommandPlan(kind=TemporalActionKind.COMPLETE_WORKFLOW, result=command.result)
    if isinstance(command, FailWorkflow):
        return TemporalCommandPlan(kind=TemporalActionKind.FAIL_WORKFLOW, failure=command.failure)
    if isinstance(command, CancelWorkflow):
        return TemporalCommandPlan(kind=TemporalActionKind.CANCEL_WORKFLOW, reason=command.reason)
    raise UnsupportedCommand(f"Unknown command type: {type(command).__name__}")


def translate_commands(
    commands: tuple[WorkflowCommand, ...],
    capabilities: BackendCapabilities,
) -> tuple[TemporalCommandPlan, ...]:
    """Translate every command, raising UnsupportedCommand on the first gap."""
    return tuple(translate_command(command, capabilities) for command in commands)


def _translate_activity(
    command: ScheduleActivity,
    capabilities: BackendCapabilities,
) -> TemporalCommandPlan:
    if command.execution_mode.value not in capabilities.supported_execution_modes:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support execution mode "
            f"{command.execution_mode.value!r}."
        )
    if command.priority != 0 and not capabilities.supports_activity_priority:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support activity priority."
        )
    if command.delay_seconds > 0 and not capabilities.supports_activity_delay:
        raise UnsupportedCommand(f"Backend {capabilities.name!r} does not support activity delay.")
    if command.partition_key is not None and not capabilities.supports_queue_partitions:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support queue partitions."
        )
    timeout = command.timeout
    if (
        timeout.schedule_to_close_seconds is not None
        and not capabilities.supports_schedule_to_close_timeout
    ):
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support schedule-to-close timeouts."
        )
    if (
        timeout.start_to_close_seconds is not None
        and not capabilities.supports_start_to_close_timeout
    ):
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support start-to-close timeouts."
        )
    if timeout.heartbeat_seconds is not None and not capabilities.supports_heartbeat_timeout:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support heartbeat timeouts."
        )
    return TemporalCommandPlan(
        kind=TemporalActionKind.EXECUTE_ACTIVITY,
        activity_key=command.activity_key,
        activity_name=command.activity_name,
        activity_version=command.activity_version,
        input=command.input,
        retry_policy=command.retry_policy,
        schedule_to_close_seconds=timeout.schedule_to_close_seconds,
        start_to_close_seconds=timeout.start_to_close_seconds,
        heartbeat_seconds=timeout.heartbeat_seconds,
    )
