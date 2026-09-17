"""Framework-neutral workflow execution value objects."""

from __future__ import annotations

import random as _random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ace.exceptions import ActivityCancelled, InvalidAttempt, InvalidPolicy
from ace.json_types import JsonObject, JsonValue

if TYPE_CHECKING:
    from datetime import datetime


class WorkflowStatus(StrEnum):
    # Reserved for persistence adapters before the initial transition commits.
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    CANCELLING = "CANCELLING"
    # BLOCKED is nonterminal but not claimable — workflow awaits operator intervention.
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}

    @property
    def is_claimable(self) -> bool:
        """Whether a dispatcher can process events for this workflow."""
        return self in {self.RUNNING, self.WAITING, self.CANCELLING}


class WorkflowEventType(StrEnum):
    WORKFLOW_STARTED = "workflow.started"
    ACTIVITY_COMPLETED = "activity.completed"
    ACTIVITY_FAILED = "activity.failed"
    ACTIVITY_CANCELLED = "activity.cancelled"
    ACTIVITY_GROUP_COMPLETED = "activity_group.completed"
    ACTIVITY_GROUP_FAILED = "activity_group.failed"
    TIMER_FIRED = "timer.fired"
    CANCELLATION_REQUESTED = "workflow.cancellation_requested"
    # 1.0 events for timeout and deadline handling
    ACTIVITY_TIMED_OUT = "activity.timed_out"
    WORKFLOW_DEADLINE_EXCEEDED = "workflow.deadline_exceeded"
    SIGNAL_RECEIVED = "workflow.signal_received"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 300.0
    # Fraction of the computed delay to randomize by, e.g. 0.2 spreads the
    # delay uniformly over +/-20%. Defaults to 0.0 (no jitter) so existing
    # callers see byte-for-byte identical delays until they opt in. Use a
    # nonzero value for any retry population that can fail in lockstep
    # (parallel activity groups, shared upstream dependency) to avoid
    # synchronized "retry storm" spikes.
    jitter_fraction: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise InvalidPolicy("RetryPolicy.max_attempts must be at least 1.")
        if self.initial_delay_seconds < 0:
            raise InvalidPolicy("RetryPolicy.initial_delay_seconds cannot be negative.")
        if self.backoff_multiplier < 1:
            raise InvalidPolicy("RetryPolicy.backoff_multiplier must be at least 1.")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise InvalidPolicy(
                "RetryPolicy.max_delay_seconds cannot be less than initial_delay_seconds."
            )
        if not 0.0 <= self.jitter_fraction <= 1.0:
            raise InvalidPolicy("RetryPolicy.jitter_fraction must be between 0.0 and 1.0.")

    def delay_after(
        self, failed_attempt: int, *, rng: Callable[[], float] = _random.random
    ) -> float:
        if failed_attempt < 1:
            raise InvalidAttempt("failed_attempt must be at least 1.")
        delay = self.initial_delay_seconds * self.backoff_multiplier ** (failed_attempt - 1)
        delay = min(delay, self.max_delay_seconds)
        if self.jitter_fraction:
            span = delay * self.jitter_fraction
            delay = delay + span * (2.0 * rng() - 1.0)
        return min(max(0.0, delay), self.max_delay_seconds)


@dataclass(frozen=True)
class WorkflowFailure:
    error_type: str
    message: str
    details: JsonObject = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    occurred_at: datetime
    payload: JsonObject = field(default_factory=dict)
    actor: str | None = None


@dataclass(frozen=True)
class WorkflowSnapshot:
    run_id: str
    namespace: str
    workflow_name: str
    workflow_version: str
    status: WorkflowStatus
    input: JsonObject
    state: JsonObject
    result: JsonValue = None
    failure: WorkflowFailure | None = None
    last_event_sequence: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class WorkflowContext:
    run_id: str
    now: datetime


Heartbeat = Callable[[JsonObject | None], None]
CancellationCheck = Callable[[], bool]


@dataclass(frozen=True)
class ActivityContext:
    activity_run_id: str
    operation_id: str
    workflow_run_id: str | None
    attempt: int
    heartbeat: Heartbeat
    cancellation_requested: CancellationCheck

    def check_cancelled(self) -> None:
        if self.cancellation_requested():
            raise ActivityCancelled(
                f"Activity {self.activity_run_id!r} was cancelled during attempt {self.attempt}."
            )
