from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from ace import (
    ActivityGroupCompletionPolicy,
    CompleteWorkflow,
    ConcurrentTransition,
    InMemoryExecutionStore,
    InvalidTransition,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowContext,
    WorkflowEngine,
    WorkflowEvent,
    WorkflowRegistry,
    WorkflowTransition,
)
from ace.json_types import JsonObject

from tests.helpers import FixedClock, SequenceIds, build_engine

FROZEN_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class InvalidWorkflow:
    name: str = "invalid"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleActivity(activity_key="same", activity_name="one"),
                ScheduleActivity(activity_key="same", activity_name="two"),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        return WorkflowTransition(
            state=state,
            commands=(
                ScheduleActivity(activity_key="work", activity_name="work"),
                CompleteWorkflow(),
            ),
        )


@dataclass(frozen=True)
class InvalidTerminalWorkflow:
    name: str = "invalid-terminal"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(state=input)

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        return WorkflowTransition(
            state=state,
            commands=(
                ScheduleActivity(activity_key="work", activity_name="work"),
                CompleteWorkflow(),
            ),
        )


@dataclass(frozen=True)
class NaiveTimerWorkflow:
    name: str = "naive-timer"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleTimer(
                    timer_key="wake",
                    fire_at=datetime(2026, 7, 14, 12, 0),
                ),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        return WorkflowTransition(state=state)


def test_transition_rejects_duplicate_command_keys() -> None:
    registry = WorkflowRegistry()
    registry.register(InvalidWorkflow())
    engine = WorkflowEngine(
        registry,
        InMemoryExecutionStore(),
        FixedClock(),
        SequenceIds("run-1", "event-1"),
    )

    with pytest.raises(InvalidTransition, match="Activity keys must be unique"):
        engine.start("invalid", {})


def test_transition_rejects_work_scheduled_with_terminal_command() -> None:
    registry = WorkflowRegistry()
    registry.register(InvalidTerminalWorkflow())
    engine = WorkflowEngine(
        registry,
        InMemoryExecutionStore(),
        FixedClock(),
        SequenceIds("run-1", "event-1", "event-2"),
    )
    started = engine.start("invalid-terminal", {})

    with pytest.raises(InvalidTransition, match="cannot be combined"):
        engine.handle_event(started.run_id, "advance")


def test_transition_rejects_naive_timer() -> None:
    registry = WorkflowRegistry()
    registry.register(NaiveTimerWorkflow())
    engine = WorkflowEngine(
        registry,
        InMemoryExecutionStore(),
        FixedClock(),
        SequenceIds("run-1", "event-1"),
    )

    with pytest.raises(InvalidTransition, match="timezone-aware"):
        engine.start("naive-timer", {})


def test_store_rejects_stale_sequence_and_protects_internal_state() -> None:
    engine, store = build_engine()
    started = engine.start("example", {"subject": "risk-1"})
    local = store.load(started.run_id)
    local.state["phase"] = "mutated"

    assert store.load(started.run_id).state["phase"] == "activity"
    event = WorkflowEvent(
        event_id="stale-event",
        run_id=started.run_id,
        sequence=2,
        event_type="stale",
        occurred_at=FROZEN_NOW,
    )
    with pytest.raises(ConcurrentTransition, match="expected sequence 0"):
        store.commit(0, started, event, ())


# ---------------------------------------------------------------------------
# Activity group validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlankGroupKeyWorkflow:
    name: str = "blank-group-key"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleActivityGroup(
                    group_key="  ",
                    activities=(ScheduleActivity(activity_key="a", activity_name="work"),),
                ),
            ),
        )

    def advance(
        self, state: JsonObject, event: WorkflowEvent, context: WorkflowContext
    ) -> WorkflowTransition:
        return WorkflowTransition(state=state)


@dataclass(frozen=True)
class EmptyGroupWorkflow:
    name: str = "empty-group"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleActivityGroup(
                    group_key="empty",
                    activities=(),
                ),
            ),
        )

    def advance(
        self, state: JsonObject, event: WorkflowEvent, context: WorkflowContext
    ) -> WorkflowTransition:
        return WorkflowTransition(state=state)


@dataclass(frozen=True)
class DuplicateMemberKeyWorkflow:
    name: str = "dup-member"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleActivityGroup(
                    group_key="grp",
                    activities=(
                        ScheduleActivity(activity_key="same", activity_name="one"),
                        ScheduleActivity(activity_key="same", activity_name="two"),
                    ),
                ),
            ),
        )

    def advance(
        self, state: JsonObject, event: WorkflowEvent, context: WorkflowContext
    ) -> WorkflowTransition:
        return WorkflowTransition(state=state)


@dataclass(frozen=True)
class DuplicateGroupKeyWorkflow:
    name: str = "dup-group"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleActivityGroup(
                    group_key="grp",
                    activities=(ScheduleActivity(activity_key="a", activity_name="work"),),
                ),
                ScheduleActivityGroup(
                    group_key="grp",
                    activities=(ScheduleActivity(activity_key="b", activity_name="work"),),
                ),
            ),
        )

    def advance(
        self, state: JsonObject, event: WorkflowEvent, context: WorkflowContext
    ) -> WorkflowTransition:
        return WorkflowTransition(state=state)


@dataclass(frozen=True)
class CrossCollisionWorkflow:
    """Standalone activity key collides with a group member key."""

    name: str = "cross-collision"
    version: str = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state=input,
            commands=(
                ScheduleActivity(activity_key="shared", activity_name="standalone"),
                ScheduleActivityGroup(
                    group_key="grp",
                    activities=(ScheduleActivity(activity_key="shared", activity_name="grouped"),),
                ),
            ),
        )

    def advance(
        self, state: JsonObject, event: WorkflowEvent, context: WorkflowContext
    ) -> WorkflowTransition:
        return WorkflowTransition(state=state)


def test_group_rejects_blank_group_key() -> None:
    registry = WorkflowRegistry()
    registry.register(BlankGroupKeyWorkflow())
    engine = WorkflowEngine(
        registry, InMemoryExecutionStore(), FixedClock(), SequenceIds("run-1", "event-1")
    )
    with pytest.raises(InvalidTransition, match="Group key cannot be blank"):
        engine.start("blank-group-key", {})


def test_group_rejects_empty_activities() -> None:
    registry = WorkflowRegistry()
    registry.register(EmptyGroupWorkflow())
    engine = WorkflowEngine(
        registry, InMemoryExecutionStore(), FixedClock(), SequenceIds("run-1", "event-1")
    )
    with pytest.raises(InvalidTransition, match="must contain at least one activity"):
        engine.start("empty-group", {})


def test_group_rejects_duplicate_member_keys() -> None:
    registry = WorkflowRegistry()
    registry.register(DuplicateMemberKeyWorkflow())
    engine = WorkflowEngine(
        registry, InMemoryExecutionStore(), FixedClock(), SequenceIds("run-1", "event-1")
    )
    with pytest.raises(InvalidTransition, match="within group"):
        engine.start("dup-member", {})


def test_group_rejects_duplicate_group_keys() -> None:
    registry = WorkflowRegistry()
    registry.register(DuplicateGroupKeyWorkflow())
    engine = WorkflowEngine(
        registry, InMemoryExecutionStore(), FixedClock(), SequenceIds("run-1", "event-1")
    )
    with pytest.raises(InvalidTransition, match="Group keys must be unique"):
        engine.start("dup-group", {})


def test_group_rejects_cross_collision_with_standalone() -> None:
    registry = WorkflowRegistry()
    registry.register(CrossCollisionWorkflow())
    engine = WorkflowEngine(
        registry, InMemoryExecutionStore(), FixedClock(), SequenceIds("run-1", "event-1")
    )
    with pytest.raises(InvalidTransition, match="Activity keys must be unique"):
        engine.start("cross-collision", {})


def test_schedule_activity_group_is_frozen_and_json_compatible() -> None:
    group = ScheduleActivityGroup(
        group_key="test",
        activities=(
            ScheduleActivity(activity_key="a", activity_name="work"),
            ScheduleActivity(activity_key="b", activity_name="work"),
        ),
    )
    # Frozen — cannot assign attributes.
    with pytest.raises(AttributeError):
        group.group_key = "changed"  # type: ignore[misc]

    # JSON-compatible: completion_policy is a string.
    assert group.completion_policy == "ALL_SUCCESS"
    assert isinstance(group.completion_policy, str)

    # Default completion policy is ALL_SUCCESS.
    assert group.completion_policy == ActivityGroupCompletionPolicy.ALL_SUCCESS
