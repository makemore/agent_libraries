"""Tests for workflow replay verification."""

from datetime import datetime, timezone

import pytest

from ace import (
    CompleteWorkflow,
    ScheduleActivity,
    WorkflowContext,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowSnapshot,
    WorkflowStatus,
    WorkflowTransition,
)
from ace.replay import ReplayStatus, WorkflowReplayVerifier


class DeterministicWorkflow:
    """A deterministic workflow that always produces the same commands."""

    name = "deterministic_workflow"
    version = "1"

    def start(self, input, context):
        return WorkflowTransition(
            state={"step": "started", "value": input.get("value", 0)},
            commands=(
                ScheduleActivity(
                    activity_key="compute",
                    activity_name="compute_value",
                    input=input,
                ),
            ),
        )

    def advance(self, state, event, context):
        if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:
            result = event.payload.get("result")
            return WorkflowTransition(
                state={"step": "completed", "result": result},
                commands=(CompleteWorkflow(result=result),),
            )
        return WorkflowTransition(state=state)


class NondeterministicWorkflow:
    """A workflow that produces different commands on replay."""

    name = "nondeterministic_workflow"
    version = "1"
    _call_count = 0

    def start(self, input, context):
        self._call_count += 1
        return WorkflowTransition(
            state={"step": "started", "call_count": self._call_count},
            commands=(
                ScheduleActivity(
                    activity_key=f"activity_{self._call_count}",
                    activity_name="compute_value",
                    input=input,
                ),
            ),
        )

    def advance(self, state, event, context):
        return WorkflowTransition(state=state, commands=(CompleteWorkflow(),))


class TestWorkflowReplayVerifier:
    def test_deterministic_workflow_passes_replay(self):
        workflow = DeterministicWorkflow()
        verifier = WorkflowReplayVerifier()

        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        start_event = WorkflowEvent(
            event_id="evt-1",
            run_id="run-1",
            sequence=1,
            event_type=WorkflowEventType.WORKFLOW_STARTED,
            occurred_at=now,
            payload={"workflow_name": "deterministic_workflow", "workflow_version": "1"},
        )

        # Expected commands from start
        expected_commands = {
            1: (
                ScheduleActivity(
                    activity_key="compute",
                    activity_name="compute_value",
                    input={"value": 42},
                ),
            ),
        }

        snapshot = WorkflowSnapshot(
            run_id="run-1",
            namespace="default",
            workflow_name="deterministic_workflow",
            workflow_version="1",
            status=WorkflowStatus.RUNNING,
            input={"value": 42},
            state={"step": "started", "value": 42},
            last_event_sequence=1,
        )

        report = verifier.replay(
            workflow,
            snapshot,
            (start_event,),
            expected_commands,
        )

        assert report.status == ReplayStatus.PASSED
        assert len(report.events) == 1
        assert report.events[0].status == ReplayStatus.PASSED

    def test_nondeterministic_workflow_fails_replay(self):
        workflow = NondeterministicWorkflow()
        verifier = WorkflowReplayVerifier()

        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        start_event = WorkflowEvent(
            event_id="evt-1",
            run_id="run-1",
            sequence=1,
            event_type=WorkflowEventType.WORKFLOW_STARTED,
            occurred_at=now,
            payload={},
        )

        # Original execution produced activity_1, but replay will produce activity_2
        expected_commands = {
            1: (
                ScheduleActivity(
                    activity_key="activity_1",
                    activity_name="compute_value",
                    input={"value": 42},
                ),
            ),
        }

        snapshot = WorkflowSnapshot(
            run_id="run-1",
            namespace="default",
            workflow_name="nondeterministic_workflow",
            workflow_version="1",
            status=WorkflowStatus.RUNNING,
            input={"value": 42},
            state={"step": "started", "call_count": 1},
            last_event_sequence=1,
        )

        report = verifier.replay(
            workflow,
            snapshot,
            (start_event,),
            expected_commands,
        )

        # First call increments to 2, so commands differ
        assert report.status == ReplayStatus.FAILED
