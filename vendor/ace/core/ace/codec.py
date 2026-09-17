"""Canonical serialization for workflow commands and events.

The codec provides deterministic JSON representations of commands and events
for replay verification and history persistence. Serialization is stable:
the same command always produces the same JSON structure.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ace.commands import (
    ActivityExecutionMode,
    ActivityGroupCompletionPolicy,
    ActivityTimeoutConfig,
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowCommand,
)
from ace.models import RetryPolicy, WorkflowFailure

if TYPE_CHECKING:
    from ace.json_types import JsonObject, JsonValue


def serialize_command(command: WorkflowCommand) -> JsonObject:
    """Serialize a workflow command to a canonical JSON-compatible dict."""
    if isinstance(command, ScheduleActivity):
        return {
            "type": "ScheduleActivity",
            "activity_key": command.activity_key,
            "activity_name": command.activity_name,
            "activity_version": command.activity_version,
            "input": command.input,
            "queue": command.queue,
            "retry_policy": _serialize_retry_policy(command.retry_policy),
            "idempotency_key": command.idempotency_key,
            "priority": command.priority,
            "delay_seconds": command.delay_seconds,
            "partition_key": command.partition_key,
            "execution_mode": command.execution_mode.value,
            "timeout": _serialize_timeout_config(command.timeout),
        }
    if isinstance(command, ScheduleActivityGroup):
        return {
            "type": "ScheduleActivityGroup",
            "group_key": command.group_key,
            "activities": [serialize_command(member) for member in command.activities],
            "completion_policy": command.completion_policy.value,
        }
    if isinstance(command, ScheduleTimer):
        return {
            "type": "ScheduleTimer",
            "timer_key": command.timer_key,
            "fire_at": command.fire_at.isoformat(),
            "payload": command.payload,
        }
    if isinstance(command, CompleteWorkflow):
        return {
            "type": "CompleteWorkflow",
            "result": command.result,
        }
    if isinstance(command, FailWorkflow):
        return {
            "type": "FailWorkflow",
            "failure": _serialize_failure(command.failure),
        }
    if isinstance(command, CancelWorkflow):
        return {
            "type": "CancelWorkflow",
            "reason": command.reason,
        }
    raise ValueError(f"Unknown command type: {type(command).__name__}")


def serialize_commands(commands: tuple[WorkflowCommand, ...]) -> list[JsonObject]:
    """Serialize a tuple of commands to a list of JSON-compatible dicts."""
    return [serialize_command(cmd) for cmd in commands]


def serialize_commands_json(commands: tuple[WorkflowCommand, ...]) -> str:
    """Serialize commands to a canonical JSON string."""
    return json.dumps(serialize_commands(commands), sort_keys=True, separators=(",", ":"))


def _serialize_retry_policy(policy: RetryPolicy) -> JsonObject:
    return {
        "max_attempts": policy.max_attempts,
        "initial_delay_seconds": policy.initial_delay_seconds,
        "backoff_multiplier": policy.backoff_multiplier,
        "max_delay_seconds": policy.max_delay_seconds,
        "jitter_fraction": policy.jitter_fraction,
    }


def _serialize_timeout_config(config: ActivityTimeoutConfig) -> JsonObject:
    return {
        "schedule_to_close_seconds": config.schedule_to_close_seconds,
        "start_to_close_seconds": config.start_to_close_seconds,
        "heartbeat_seconds": config.heartbeat_seconds,
    }


def _serialize_failure(failure: WorkflowFailure) -> JsonObject:
    return {
        "error_type": failure.error_type,
        "message": failure.message,
        "details": failure.details,
    }


def deserialize_commands(data: list[Any]) -> tuple[WorkflowCommand, ...]:
    """Deserialize a list of command dicts to a tuple of command objects."""
    return tuple(deserialize_command(item) for item in data)


def deserialize_command(data: JsonObject) -> WorkflowCommand:
    """Deserialize a command dict to a command object."""
    from datetime import datetime

    cmd_type = data.get("type")
    if cmd_type == "ScheduleActivity":
        return ScheduleActivity(
            activity_key=data["activity_key"],
            activity_name=data["activity_name"],
            activity_version=data.get("activity_version", "1"),
            input=data.get("input", {}),
            queue=data.get("queue", "medium"),
            retry_policy=_deserialize_retry_policy(data.get("retry_policy", {})),
            idempotency_key=data.get("idempotency_key"),
            priority=data.get("priority", 0),
            delay_seconds=data.get("delay_seconds", 0.0),
            partition_key=data.get("partition_key"),
            execution_mode=ActivityExecutionMode(
                data.get("execution_mode", ActivityExecutionMode.STANDARD.value)
            ),
            timeout=_deserialize_timeout_config(data.get("timeout", {})),
        )
    if cmd_type == "ScheduleActivityGroup":
        return ScheduleActivityGroup(
            group_key=data["group_key"],
            activities=tuple(deserialize_command(member) for member in data.get("activities", [])),
            completion_policy=ActivityGroupCompletionPolicy(
                data.get("completion_policy", ActivityGroupCompletionPolicy.ALL_SUCCESS.value)
            ),
        )
    if cmd_type == "ScheduleTimer":
        fire_at = data["fire_at"]
        if isinstance(fire_at, str):
            fire_at = datetime.fromisoformat(fire_at)
        return ScheduleTimer(
            timer_key=data["timer_key"],
            fire_at=fire_at,
            payload=data.get("payload", {}),
        )
    if cmd_type == "CompleteWorkflow":
        return CompleteWorkflow(result=data.get("result"))
    if cmd_type == "FailWorkflow":
        return FailWorkflow(failure=_deserialize_failure(data["failure"]))
    if cmd_type == "CancelWorkflow":
        return CancelWorkflow(reason=data["reason"])
    raise ValueError(f"Unknown command type: {cmd_type!r}")


def _json_int(value: JsonValue | None, default: int) -> int:
    """Coerce a JSON-decoded value to int; falls back to `default` for
    anything that isn't an int-like scalar (bool is deliberately excluded
    so a stray `true`/`false` doesn't silently become 1/0)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    return int(value)


def _json_float(value: JsonValue | None, default: float) -> float:
    """Coerce a JSON-decoded value to float; falls back to `default` for
    anything that isn't a numeric-like scalar (bool excluded, see above)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    return float(value)


def _deserialize_retry_policy(data: JsonObject) -> RetryPolicy:
    if not data:
        return RetryPolicy()
    defaults = RetryPolicy()
    return RetryPolicy(
        max_attempts=_json_int(data.get("max_attempts"), defaults.max_attempts),
        initial_delay_seconds=_json_float(
            data.get("initial_delay_seconds"), defaults.initial_delay_seconds
        ),
        backoff_multiplier=_json_float(data.get("backoff_multiplier"), defaults.backoff_multiplier),
        max_delay_seconds=_json_float(data.get("max_delay_seconds"), defaults.max_delay_seconds),
        jitter_fraction=_json_float(data.get("jitter_fraction"), defaults.jitter_fraction),
    )


def _deserialize_timeout_config(data: JsonObject) -> ActivityTimeoutConfig:
    if not data:
        return ActivityTimeoutConfig()
    return ActivityTimeoutConfig(
        schedule_to_close_seconds=data.get("schedule_to_close_seconds"),
        start_to_close_seconds=data.get("start_to_close_seconds"),
        heartbeat_seconds=data.get("heartbeat_seconds"),
    )


def _deserialize_failure(data: JsonObject) -> WorkflowFailure:
    return WorkflowFailure(
        error_type=data["error_type"],
        message=data["message"],
        details=data.get("details", {}),
    )
