from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import count
from threading import Lock

from ace import (
    CancelWorkflow,
    CompleteWorkflow,
    InMemoryExecutionStore,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowContext,
    WorkflowEngine,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowRegistry,
    WorkflowStatus,
    WorkflowTransition,
)
from ace.json_types import JsonObject

FROZEN_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


class FixedClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class NaiveClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 14, 12, 0)


class SequenceIds:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)
        self._issued = 0

    def new_id(self) -> str:
        try:
            value = next(self._values)
        except StopIteration as exc:
            raise AssertionError(f"SequenceIds exhausted after {self._issued} IDs.") from exc
        self._issued += 1
        return value


class ThreadSafeIds:
    def __init__(self) -> None:
        self._values = count(1)
        self._lock = Lock()

    def new_id(self) -> str:
        with self._lock:
            return f"generated-{next(self._values)}"


@dataclass(frozen=True)
class ExampleWorkflow:
    name: str = "example"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"phase": "activity", "subject": input["subject"]},
            commands=(
                ScheduleActivity(
                    activity_key="extract",
                    activity_name="example.extract",
                    input={"subject": input["subject"]},
                    idempotency_key=f"{context.run_id}:extract",
                ),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(
                state={**state, "phase": "cancelled"},
                commands=(CancelWorkflow(reason=str(event.payload["reason"])),),
            )
        if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:
            return WorkflowTransition(
                state={**state, "phase": "timer"},
                commands=(
                    ScheduleTimer(
                        timer_key="follow-up",
                        fire_at=context.now + timedelta(minutes=5),
                    ),
                ),
            )
        return WorkflowTransition(
            state={**state, "phase": "complete"},
            commands=(CompleteWorkflow(result={"subject": state["subject"]}),),
        )


@dataclass(frozen=True)
class DrainingWorkflow:
    name: str = "draining"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"phase": "activity"},
            commands=(ScheduleActivity(activity_key="work", activity_name="example.work"),),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(
                state={"phase": "draining"},
                status=WorkflowStatus.CANCELLING,
            )
        return WorkflowTransition(
            state={"phase": "cancelled"},
            commands=(CancelWorkflow(reason="drain complete"),),
        )


@dataclass(frozen=True)
class GroupWorkflow:
    """Synthetic workflow that schedules one activity group, then completes."""

    name: str = "group-example"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"phase": "group"},
            commands=(
                ScheduleActivityGroup(
                    group_key="parallel-extract",
                    activities=(
                        ScheduleActivity(
                            activity_key="extract-a",
                            activity_name="example.extract",
                            input={"part": "a"},
                        ),
                        ScheduleActivity(
                            activity_key="extract-b",
                            activity_name="example.extract",
                            input={"part": "b"},
                        ),
                    ),
                ),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.ACTIVITY_GROUP_COMPLETED:
            return WorkflowTransition(
                state={**state, "phase": "complete"},
                commands=(CompleteWorkflow(result=event.payload.get("results")),),
            )
        if event.event_type == WorkflowEventType.ACTIVITY_GROUP_FAILED:
            return WorkflowTransition(
                state={**state, "phase": "failed"},
                commands=(CompleteWorkflow(result={"partial": event.payload.get("results")}),),
            )
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(
                state={**state, "phase": "cancelled"},
                commands=(CancelWorkflow(reason=str(event.payload["reason"])),),
            )
        return WorkflowTransition(state=state)


def build_engine() -> tuple[WorkflowEngine, InMemoryExecutionStore]:
    registry = WorkflowRegistry()
    registry.register(ExampleWorkflow())
    store = InMemoryExecutionStore()
    ids = SequenceIds("run-1", "event-1", "event-2", "event-3")
    return WorkflowEngine(registry, store, FixedClock(), ids), store


def build_group_engine(
    *ids: str,
) -> tuple[WorkflowEngine, InMemoryExecutionStore]:
    registry = WorkflowRegistry()
    registry.register(GroupWorkflow())
    store = InMemoryExecutionStore()
    return WorkflowEngine(registry, store, FixedClock(), SequenceIds(*ids)), store
