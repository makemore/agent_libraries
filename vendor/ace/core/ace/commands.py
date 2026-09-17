"""Commands emitted by workflow transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, TypeAlias

from ace.models import RetryPolicy

if TYPE_CHECKING:
    from datetime import datetime

    from ace.json_types import JsonObject, JsonValue
    from ace.models import WorkflowFailure, WorkflowStatus


class ActivityGroupCompletionPolicy(StrEnum):
    """How a group determines success or failure from its members."""

    # Fail-fast: the first permanent member failure cancels unclaimed
    # siblings and immediately fails the group.
    ALL_SUCCESS = "ALL_SUCCESS"
    # Settle-all: every member runs to a terminal status (SUCCEEDED,
    # FAILED, or CANCELLED) before the group settles. Siblings are never
    # cancelled on a member failure. The group always emits a single
    # ACTIVITY_GROUP_COMPLETED event carrying both `results` (succeeded
    # members) and `failures` (failed/cancelled members) — the workflow's
    # own transition logic decides whether partial failure is acceptable
    # ("continue_on_failure"-style semantics), the engine does not decide
    # this unilaterally.
    WAIT_ALL = "WAIT_ALL"


class ActivityExecutionMode(StrEnum):
    """Execution mode for activity invocation."""

    # Standard at-least-once execution with external side effects allowed.
    STANDARD = "STANDARD"
    # Same-database transactional execution - callable writes commit atomically
    # with activity completion. No external side effects or network calls allowed.
    TRANSACTIONAL = "TRANSACTIONAL"


@dataclass(frozen=True)
class ActivityTimeoutConfig:
    """Timeout configuration for activity execution.

    - schedule_to_close_seconds: Maximum time from scheduling to completion (all attempts).
    - start_to_close_seconds: Maximum time from attempt start to completion (per attempt).
    - heartbeat_seconds: Maximum time between heartbeats (per attempt).

    Constraints:
    - schedule_to_close_seconds >= start_to_close_seconds (if both set)
    - heartbeat_seconds < start_to_close_seconds (if both set)
    """

    schedule_to_close_seconds: float | None = None
    start_to_close_seconds: float | None = None
    heartbeat_seconds: float | None = None

    def __post_init__(self) -> None:
        from ace.exceptions import InvalidPolicy

        if self.schedule_to_close_seconds is not None and self.schedule_to_close_seconds <= 0:
            raise InvalidPolicy("schedule_to_close_seconds must be positive.")
        if self.start_to_close_seconds is not None and self.start_to_close_seconds <= 0:
            raise InvalidPolicy("start_to_close_seconds must be positive.")
        if self.heartbeat_seconds is not None and self.heartbeat_seconds <= 0:
            raise InvalidPolicy("heartbeat_seconds must be positive.")
        if (
            self.schedule_to_close_seconds is not None
            and self.start_to_close_seconds is not None
            and self.schedule_to_close_seconds < self.start_to_close_seconds
        ):
            raise InvalidPolicy("schedule_to_close_seconds must be >= start_to_close_seconds.")
        if (
            self.heartbeat_seconds is not None
            and self.start_to_close_seconds is not None
            and self.heartbeat_seconds >= self.start_to_close_seconds
        ):
            raise InvalidPolicy("heartbeat_seconds must be < start_to_close_seconds.")


@dataclass(frozen=True)
class ScheduleActivity:
    activity_key: str
    activity_name: str
    input: JsonObject = field(default_factory=dict)
    activity_version: str = "1"
    queue: str = "medium"
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    idempotency_key: str | None = None
    # 1.0 additions
    priority: int = 0
    delay_seconds: float = 0.0
    partition_key: str | None = None
    execution_mode: ActivityExecutionMode = ActivityExecutionMode.STANDARD
    timeout: ActivityTimeoutConfig = field(default_factory=ActivityTimeoutConfig)


@dataclass(frozen=True)
class ScheduleTimer:
    timer_key: str
    fire_at: datetime
    payload: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class CompleteWorkflow:
    result: JsonValue = None


@dataclass(frozen=True)
class FailWorkflow:
    failure: WorkflowFailure


@dataclass(frozen=True)
class ScheduleActivityGroup:
    """Atomically schedule a group of activities with a completion policy."""

    group_key: str
    activities: tuple[ScheduleActivity, ...]
    completion_policy: ActivityGroupCompletionPolicy = ActivityGroupCompletionPolicy.ALL_SUCCESS


@dataclass(frozen=True)
class CancelWorkflow:
    reason: str


WorkflowCommand: TypeAlias = (
    ScheduleActivity
    | ScheduleActivityGroup
    | ScheduleTimer
    | CompleteWorkflow
    | FailWorkflow
    | CancelWorkflow
)


@dataclass(frozen=True)
class WorkflowTransition:
    state: JsonObject
    commands: tuple[WorkflowCommand, ...] = ()
    status: WorkflowStatus | None = None
