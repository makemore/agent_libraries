"""End-to-end adapter runtime tests without the Temporal SDK."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from ace.commands import CancelWorkflow, CompleteWorkflow, ScheduleTimer, WorkflowTransition
from ace.models import WorkflowEventType, WorkflowStatus
from ace.registry import WorkflowRegistry
from ace.testing.backend_contract import SimpleWorkflow

from ace_temporal import AceTemporalAdapter, TemporalActionKind, build_temporal_workflow

if TYPE_CHECKING:
    from ace.json_types import JsonObject
    from ace.models import WorkflowContext, WorkflowEvent


class SignalTimerWorkflow:
    """Workflow exercising timers, signals, and cancellation."""

    name = "signal_timer_workflow"
    version = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"step": "started", "signals": 0},
            commands=(
                ScheduleTimer(
                    timer_key="timer_1",
                    fire_at=context.now + timedelta(seconds=60),
                ),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.TIMER_FIRED:
            return WorkflowTransition(state={**state, "step": "timer_fired"})
        if event.event_type == WorkflowEventType.SIGNAL_RECEIVED:
            signals = state["signals"] + 1
            if event.payload.get("finish"):
                return WorkflowTransition(
                    state={**state, "signals": signals},
                    commands=(CompleteWorkflow(result={"signals": signals}),),
                )
            return WorkflowTransition(state={**state, "signals": signals})
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(
                state=state,
                commands=(CancelWorkflow(reason=event.payload.get("reason", "cancelled")),),
            )
        return WorkflowTransition(state=state)


@pytest.fixture
def adapter() -> AceTemporalAdapter:
    registry = WorkflowRegistry()
    registry.register(SimpleWorkflow())
    registry.register(SignalTimerWorkflow())
    return AceTemporalAdapter(registry)


def test_activity_completion_completes_workflow(adapter: AceTemporalAdapter) -> None:
    run_id = adapter.create_workflow_run("simple_test_workflow", "1", {"value": 42})
    assert run_id.startswith("temporal-")
    assert adapter.run_metadata(run_id) == {"execution_backend": "temporal"}
    adapter.complete_activity(run_id, "activity_1", {"computed": 84})
    snapshot = adapter.get_snapshot(run_id)
    assert snapshot.status == WorkflowStatus.COMPLETED
    assert snapshot.result == {"computed": 84}


def test_activity_failure_fails_workflow(adapter: AceTemporalAdapter) -> None:
    run_id = adapter.create_workflow_run("simple_test_workflow", "1", {"value": 42})
    adapter.fail_activity(run_id, "activity_1", "SomeError", "activity exploded")
    assert adapter.get_workflow_status(run_id) == WorkflowStatus.FAILED


def test_timer_event_advances_workflow(adapter: AceTemporalAdapter) -> None:
    run_id = adapter.create_workflow_run("signal_timer_workflow", "1", {})
    plans = adapter.command_plans(run_id)
    assert plans[0].kind == TemporalActionKind.START_TIMER
    assert plans[0].timer_key == "timer_1"
    adapter.handle_event(run_id, WorkflowEventType.TIMER_FIRED, payload={"timer_key": "timer_1"})
    assert adapter.get_snapshot(run_id).state["step"] == "timer_fired"


def test_duplicate_signal_applies_once(adapter: AceTemporalAdapter) -> None:
    run_id = adapter.create_workflow_run("signal_timer_workflow", "1", {})
    payload = {"signal_id": "sig-1", "name": "poke"}
    adapter.handle_event(run_id, WorkflowEventType.SIGNAL_RECEIVED, payload=payload)
    first = adapter.get_snapshot(run_id)
    adapter.handle_event(run_id, WorkflowEventType.SIGNAL_RECEIVED, payload=payload)
    second = adapter.get_snapshot(run_id)
    assert first.state["signals"] == 1
    assert second.state["signals"] == 1
    assert second.last_event_sequence == first.last_event_sequence


def test_cancellation_event_cancels_workflow(adapter: AceTemporalAdapter) -> None:
    run_id = adapter.create_workflow_run("signal_timer_workflow", "1", {})
    adapter.request_cancellation(run_id, reason="operator request")
    snapshot = adapter.get_snapshot(run_id)
    assert snapshot.status == WorkflowStatus.CANCELLED
    assert snapshot.result == {"reason": "operator request"}


def test_event_sequences_strictly_increase(adapter: AceTemporalAdapter) -> None:
    run_id = adapter.create_workflow_run("signal_timer_workflow", "1", {})
    adapter.handle_event(run_id, WorkflowEventType.TIMER_FIRED, payload={"timer_key": "timer_1"})
    adapter.handle_event(run_id, WorkflowEventType.SIGNAL_RECEIVED, payload={"signal_id": "s1"})
    adapter.handle_event(
        run_id,
        WorkflowEventType.SIGNAL_RECEIVED,
        payload={"signal_id": "s2", "finish": True},
    )
    sequences = [event.sequence for event in adapter.events(run_id)]
    assert sequences == [1, 2, 3, 4]
    assert all(later > earlier for earlier, later in zip(sequences, sequences[1:]))


def test_workflow_deadline_exceeded_fails_workflow(adapter: AceTemporalAdapter) -> None:
    assert adapter.capabilities.supports_workflow_deadline
    run_id = adapter.create_workflow_run("signal_timer_workflow", "1", {})
    snapshot = adapter.handle_event(
        run_id,
        WorkflowEventType.WORKFLOW_DEADLINE_EXCEEDED,
        payload={"message": "deadline passed", "deadline_at": "2026-01-01T00:00:00+00:00"},
    )
    assert snapshot.status == WorkflowStatus.FAILED
    assert snapshot.failure is not None
    assert snapshot.failure.error_type == "WorkflowDeadlineExceeded"
    plans = adapter.command_plans(run_id)
    assert plans[-1].kind == TemporalActionKind.FAIL_WORKFLOW


def test_build_temporal_workflow_smoke(adapter: AceTemporalAdapter) -> None:
    pytest.importorskip("temporalio")
    workflow_class = build_temporal_workflow(adapter, "simple_test_workflow", "1")
    assert isinstance(workflow_class, type)
