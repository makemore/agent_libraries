"""Translation of ACE workflow commands into DBOS-native action plans.

Commands whose semantics DBOS cannot faithfully honour are rejected with
UnsupportedCommand rather than weakly emulated.
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
    from ace.backend import BackendCapabilities
    from ace.commands import WorkflowCommand


class UnsupportedCommand(AceError):
    """A command requires semantics the target backend does not support."""


class DbosAction(StrEnum):
    """Backend-native actions a translated command maps onto."""

    ENQUEUE_STEP = "enqueue_step"
    # Timers are realized via DBOS durable sleep.
    START_TIMER = "start_timer"
    COMPLETE_WORKFLOW = "complete_workflow"
    FAIL_WORKFLOW = "fail_workflow"
    CANCEL_WORKFLOW = "cancel_workflow"


@dataclass(frozen=True)
class DbosCommandPlan:
    """A validated command paired with the DBOS action that realizes it."""

    action: DbosAction
    command: WorkflowCommand


def translate_command(
    command: WorkflowCommand,
    capabilities: BackendCapabilities,
) -> DbosCommandPlan:
    """Translate one command, raising UnsupportedCommand when not representable."""
    if isinstance(command, ScheduleActivity):
        _validate_activity(command, capabilities)
        return DbosCommandPlan(DbosAction.ENQUEUE_STEP, command)
    if isinstance(command, ScheduleActivityGroup):
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} declares no activity-group support; "
            f"group {command.group_key!r} cannot be represented."
        )
    if isinstance(command, ScheduleTimer):
        if not capabilities.supports_timers:
            raise UnsupportedCommand(f"Backend {capabilities.name!r} does not support timers.")
        return DbosCommandPlan(DbosAction.START_TIMER, command)
    if isinstance(command, CompleteWorkflow):
        return DbosCommandPlan(DbosAction.COMPLETE_WORKFLOW, command)
    if isinstance(command, FailWorkflow):
        return DbosCommandPlan(DbosAction.FAIL_WORKFLOW, command)
    if isinstance(command, CancelWorkflow):
        return DbosCommandPlan(DbosAction.CANCEL_WORKFLOW, command)
    raise UnsupportedCommand(f"Unknown command type {type(command).__name__!r}.")


def translate_commands(
    commands: tuple[WorkflowCommand, ...],
    capabilities: BackendCapabilities,
) -> tuple[DbosCommandPlan, ...]:
    """Translate a transition's commands, rejecting any unsupported one."""
    return tuple(translate_command(command, capabilities) for command in commands)


def _validate_activity(
    command: ScheduleActivity,
    capabilities: BackendCapabilities,
) -> None:
    if command.execution_mode.value not in capabilities.supported_execution_modes:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support execution mode "
            f"{command.execution_mode.value!r}."
        )
    if command.priority != 0 and not capabilities.supports_activity_priority:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support activity priority; "
            f"activity {command.activity_key!r} requested priority {command.priority}."
        )
    if command.delay_seconds > 0 and not capabilities.supports_activity_delay:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support delayed activities; "
            f"activity {command.activity_key!r} requested delay {command.delay_seconds}s."
        )
    if command.partition_key is not None and not capabilities.supports_queue_partitions:
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support queue partitions; "
            f"activity {command.activity_key!r} requested partition "
            f"{command.partition_key!r}."
        )
    if (
        command.timeout.heartbeat_seconds is not None
        and not capabilities.supports_heartbeat_timeout
    ):
        raise UnsupportedCommand(
            f"Backend {capabilities.name!r} does not support heartbeat timeouts; "
            f"activity {command.activity_key!r} requested one."
        )
