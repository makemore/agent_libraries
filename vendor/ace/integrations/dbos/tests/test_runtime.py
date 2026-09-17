"""End-to-end DBOS adapter runtime tests without the SDK installed."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from ace.commands import (
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    ScheduleActivity,
    ScheduleTimer,
    WorkflowTransition,
)
from ace.models import WorkflowEventType, WorkflowFailure, WorkflowStatus
from ace.registry import WorkflowRegistry

from ace_dbos.adapter import AceDbosAdapter, build_dbos_workflow
from ace_dbos.codec import DbosAction

if TYPE_CHECKING:
    from ace.json_types import JsonObject
    from ace.models import WorkflowContext, WorkflowEvent


class LifecycleWorkflow:
    """Exercises activities, timers, signals, and cancellation."""

    name = "lifecycle"
    version = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"signals": 0, "timer_fired": False},
            commands=(
                ScheduleActivity(activity_key="activity_1", activity_name="do_work", input=input),
                ScheduleTimer(timer_key="timeout", fire_at=context.now + timedelta(seconds=60)),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:
            return WorkflowTransition(
                state=state,
                commands=(CompleteWorkflow(result=event.payload.get("result")),),
            )
        if event.event_type == WorkflowEventType.ACTIVITY_FAILED:
            failure = WorkflowFailure(
                error_type=event.payload["error_type"],
                message=event.payload["message"],
            )
            return WorkflowTransition(state=state, commands=(FailWorkflow(failure=failure),))
        if event.event_type == WorkflowEventType.TIMER_FIRED:
            return WorkflowTransition(state={**state, "timer_fired": True})
        if event.event_type == WorkflowEventType.SIGNAL_RECEIVED:
            return WorkflowTransition(state={**state, "signals": state["signals"] + 1})
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(
                state=state,
                commands=(CancelWorkflow(reason=event.payload.get("reason", "cancelled")),),
            )
        return WorkflowTransition(state=state)


@pytest.fixture
def adapter() -> AceDbosAdapter:
    registry = WorkflowRegistry()
    registry.register(LifecycleWorkflow())
    return AceDbosAdapter(registry)


def test_start_stamps_dbos_authority(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    assert run_id.startswith("dbos-")
    assert adapter.get_authority(run_id) == {"execution_backend": "dbos"}
    actions = [plan.action for plan in adapter.list_plans(run_id)]
    assert actions == [DbosAction.ENQUEUE_STEP, DbosAction.START_TIMER]


def test_activity_completion_completes_workflow(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    adapter.complete_activity(run_id, "activity_1", {"computed": 2})
    assert adapter.get_workflow_status(run_id) == WorkflowStatus.COMPLETED
    assert adapter.get_snapshot(run_id).result == {"computed": 2}


def test_activity_failure_fails_workflow(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    adapter.fail_activity(run_id, "activity_1", "Boom", "it broke")
    assert adapter.get_workflow_status(run_id) == WorkflowStatus.FAILED
    failure = adapter.get_snapshot(run_id).failure
    assert failure is not None
    assert failure.error_type == "Boom"
    assert failure.message == "it broke"


def test_timer_event_advances_state(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    adapter.handle_event(run_id, WorkflowEventType.TIMER_FIRED, {"timer_key": "timeout"})
    snapshot = adapter.get_snapshot(run_id)
    assert snapshot.state["timer_fired"] is True
    assert snapshot.status == WorkflowStatus.WAITING


def test_duplicate_signal_is_not_reapplied(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    adapter.handle_event(
        run_id,
        WorkflowEventType.SIGNAL_RECEIVED,
        {"signal": "poke"},
        source_key="signal-1",
    )
    first = adapter.get_snapshot(run_id)
    adapter.handle_event(
        run_id,
        WorkflowEventType.SIGNAL_RECEIVED,
        {"signal": "poke"},
        source_key="signal-1",
    )
    second = adapter.get_snapshot(run_id)
    assert first.state["signals"] == 1
    assert second.state["signals"] == 1
    assert second.last_event_sequence == first.last_event_sequence


def test_cancellation_event_cancels_workflow(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    adapter.request_cancellation(run_id, reason="user requested")
    assert adapter.get_workflow_status(run_id) == WorkflowStatus.CANCELLED
    assert adapter.get_snapshot(run_id).result == {"reason": "user requested"}


def test_event_sequences_strictly_increase(adapter: AceDbosAdapter) -> None:
    run_id = adapter.create_workflow_run("lifecycle", "1", {"value": 1})
    adapter.handle_event(run_id, WorkflowEventType.SIGNAL_RECEIVED, {"signal": "a"})
    adapter.handle_event(run_id, WorkflowEventType.TIMER_FIRED, {"timer_key": "timeout"})
    adapter.complete_activity(run_id, "activity_1", {"done": True})
    sequences = [event.sequence for event in adapter.list_events(run_id)]
    assert sequences == [1, 2, 3, 4]


def test_build_dbos_workflow_smoke() -> None:
    pytest.importorskip("dbos")
    registry = WorkflowRegistry()
    registry.register(LifecycleWorkflow())
    adapter = AceDbosAdapter(registry)
    workflow = build_dbos_workflow(adapter, "lifecycle", "1")
    assert callable(workflow)
