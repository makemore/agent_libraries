from dataclasses import dataclass
from datetime import UTC, datetime

from ace import (
    ActivityGroupCompletionPolicy,
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    RetryPolicy,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowContext,
    WorkflowEngine,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowFailure,
    WorkflowRegistry,
    WorkflowStatus,
    WorkflowTransition,
)
from ace.json_types import JsonObject

from ace_django.store import DjangoExecutionStore

FROZEN_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
RUN_ID = "10000000-0000-0000-0000-000000000001"
START_EVENT_ID = "20000000-0000-0000-0000-000000000001"
SECOND_EVENT_ID = "20000000-0000-0000-0000-000000000002"
THIRD_EVENT_ID = "20000000-0000-0000-0000-000000000003"


class FixedClock:
    def now(self) -> datetime:
        return FROZEN_NOW


class SequenceIds:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)

    def new_id(self) -> str:
        try:
            return next(self._values)
        except StopIteration as exc:
            raise AssertionError("ACE test ID sequence was exhausted.") from exc


@dataclass
class AdapterWorkflow:
    name: str = "adapter-test"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"phase": "activity"},
            commands=(
                ScheduleActivity(
                    activity_key="work",
                    activity_name="tests.work",
                    input=input,
                    retry_policy=RetryPolicy(
                        max_attempts=2,
                        initial_delay_seconds=1,
                        max_delay_seconds=1,
                    ),
                    idempotency_key=f"{context.run_id}:work",
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
                state={"phase": "cancelled"},
                commands=(CancelWorkflow(reason=str(event.payload["reason"])),),
            )
        if event.event_type == WorkflowEventType.ACTIVITY_FAILED:
            return WorkflowTransition(
                state={"phase": "failed"},
                commands=(
                    FailWorkflow(
                        WorkflowFailure(error_type="ActivityFailed", message="work failed")
                    ),
                ),
            )
        if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:
            return WorkflowTransition(
                state={"phase": "timer"},
                commands=(
                    ScheduleTimer(
                        timer_key="follow-up",
                        fire_at=context.now,
                    ),
                ),
            )
        return WorkflowTransition(
            state={"phase": "complete"},
            commands=(CompleteWorkflow(result={"ok": True}),),
        )


@dataclass
class FanInWorkflow:
    name: str = "fan-in-test"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"completed": 0},
            commands=(
                ScheduleActivity(activity_key="first", activity_name="tests.echo", input=input),
                ScheduleActivity(activity_key="second", activity_name="tests.echo", input=input),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        current = state["completed"]
        assert isinstance(current, int)
        completed = current + 1
        if completed == 2:
            return WorkflowTransition(
                state={"completed": completed},
                commands=(CompleteWorkflow(result={"completed": completed}),),
            )
        return WorkflowTransition(
            state={"completed": completed},
            status=WorkflowStatus.WAITING,
        )


@dataclass
class DrainingAdapterWorkflow:
    name: str = "draining-adapter-test"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"phase": "activity"},
            commands=(ScheduleActivity(activity_key="work", activity_name="tests.work"),),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(state={"phase": "draining"}, status=WorkflowStatus.CANCELLING)
        return WorkflowTransition(
            state={"phase": "cancelled"},
            commands=(CancelWorkflow(reason="drain complete"),),
        )


def build_engine(*ids: str) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register(AdapterWorkflow())
    values = ids or (RUN_ID, START_EVENT_ID, SECOND_EVENT_ID, THIRD_EVENT_ID)
    return WorkflowEngine(registry, DjangoExecutionStore(), FixedClock(), SequenceIds(*values))


def build_fan_in_engine(*ids: str) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register(FanInWorkflow())
    return WorkflowEngine(registry, DjangoExecutionStore(), FixedClock(), SequenceIds(*ids))


def build_draining_engine(*ids: str) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register(DrainingAdapterWorkflow())
    return WorkflowEngine(registry, DjangoExecutionStore(), FixedClock(), SequenceIds(*ids))


@dataclass
class GroupAdapterWorkflow:
    """Adapter workflow scheduling one ScheduleActivityGroup."""

    name: str = "group-adapter-test"
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
                            activity_name="tests.extract",
                            input={"part": "a"},
                            retry_policy=RetryPolicy(max_attempts=1),
                        ),
                        ScheduleActivity(
                            activity_key="extract-b",
                            activity_name="tests.extract",
                            input={"part": "b"},
                            retry_policy=RetryPolicy(max_attempts=1),
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
                commands=(
                    FailWorkflow(WorkflowFailure(error_type="GroupFailed", message="group failed")),
                ),
            )
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(
                state={**state, "phase": "cancelled"},
                commands=(CancelWorkflow(reason=str(event.payload["reason"])),),
            )
        return WorkflowTransition(state=state)


def build_group_engine(*ids: str) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register(GroupAdapterWorkflow())
    return WorkflowEngine(registry, DjangoExecutionStore(), FixedClock(), SequenceIds(*ids))


@dataclass
class DrainingGroupWorkflow(GroupAdapterWorkflow):
    """Group workflow that (illegally) tries to drain on cancellation."""

    name: str = "draining-group-test"

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(state={"phase": "draining"}, status=WorkflowStatus.CANCELLING)
        return super().advance(state, event, context)


def build_draining_group_engine(*ids: str) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register(DrainingGroupWorkflow())
    return WorkflowEngine(registry, DjangoExecutionStore(), FixedClock(), SequenceIds(*ids))


@dataclass
class WaitAllGroupWorkflow(GroupAdapterWorkflow):
    """Group workflow using the WAIT_ALL completion policy: settles once every
    member is terminal (regardless of outcome) and completes the workflow
    with both `results` and `failures`, demonstrating continue-on-failure."""

    name: str = "wait-all-group-test"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        transition = super().start(input, context)
        group_command = transition.commands[0]
        assert isinstance(group_command, ScheduleActivityGroup)
        return WorkflowTransition(
            state=transition.state,
            commands=(
                ScheduleActivityGroup(
                    group_key=group_command.group_key,
                    activities=group_command.activities,
                    completion_policy=ActivityGroupCompletionPolicy.WAIT_ALL,
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
                commands=(
                    CompleteWorkflow(
                        result={
                            "results": event.payload.get("results"),
                            "failures": event.payload.get("failures"),
                        }
                    ),
                ),
            )
        return super().advance(state, event, context)


def build_wait_all_group_engine(*ids: str) -> WorkflowEngine:
    registry = WorkflowRegistry()
    registry.register(WaitAllGroupWorkflow())
    return WorkflowEngine(registry, DjangoExecutionStore(), FixedClock(), SequenceIds(*ids))
