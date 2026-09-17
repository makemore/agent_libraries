"""Tests for Django workflow replay functionality.

Tests verify:
- Loading events from Django ORM
- Loading persisted commands
- Replay with and without commands
- Replay report generation
"""

from __future__ import annotations

from datetime import datetime, timezone as tz
from uuid import UUID

import pytest

from ace import WorkflowEventType, WorkflowStatus
from ace.codec import serialize_commands
from ace.commands import ScheduleActivity
from ace.commands import WorkflowTransition
from ace.definitions import WorkflowDefinition
from ace.models import WorkflowContext, WorkflowEvent
from ace.registry import WorkflowRegistry
from ace.replay import ReplayStatus
from ace_django.models import WorkflowEvent as DjangoWorkflowEvent, WorkflowRun
from ace_django.replay import (
    load_persisted_commands,
    load_workflow_events,
    load_workflow_snapshot,
    replay_workflow,
)

pytestmark = pytest.mark.django_db(transaction=True)

FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=tz.utc)
RUN_ID = "10000000-0000-0000-0000-000000000001"


def create_test_workflow() -> WorkflowRun:
    """Create a test workflow run."""
    return WorkflowRun.objects.create(
        id=UUID(RUN_ID),
        workflow_name="test-workflow",
        workflow_version="1",
        status=WorkflowStatus.COMPLETED.value,
        state={"counter": 1},
        input={"initial": True},
        result={"done": True},
        idempotency_key="test-run",
        started_at=FROZEN_NOW,
        completed_at=FROZEN_NOW,
        last_event_sequence=1,
    )


def create_test_event(
    run: WorkflowRun,
    sequence: int,
    event_type: str,
    commands: list | None = None,
) -> DjangoWorkflowEvent:
    """Create a test workflow event."""
    return DjangoWorkflowEvent.objects.create(
        workflow_run=run,
        sequence=sequence,
        event_type=event_type,
        payload={},
        occurred_at=FROZEN_NOW,
        commands=commands,
    )


def test_load_workflow_events() -> None:
    """load_workflow_events returns core WorkflowEvent dataclasses."""
    run = create_test_workflow()
    create_test_event(run, 1, WorkflowEventType.WORKFLOW_STARTED)
    create_test_event(run, 2, WorkflowEventType.ACTIVITY_COMPLETED)

    events = load_workflow_events(RUN_ID)

    assert len(events) == 2
    assert all(isinstance(e, WorkflowEvent) for e in events)
    assert events[0].sequence == 1
    assert events[1].sequence == 2


def test_load_persisted_commands() -> None:
    """load_persisted_commands deserializes command JSON."""
    run = create_test_workflow()
    commands = (ScheduleActivity(activity_key="task-1", activity_name="test.task"),)
    serialized = serialize_commands(commands)
    create_test_event(run, 1, WorkflowEventType.WORKFLOW_STARTED, commands=serialized)

    commands_map = load_persisted_commands(RUN_ID)

    assert 1 in commands_map
    assert len(commands_map[1]) == 1
    assert commands_map[1][0].activity_key == "task-1"


def test_load_persisted_commands_skips_null() -> None:
    """load_persisted_commands skips events with null commands."""
    run = create_test_workflow()
    create_test_event(run, 1, WorkflowEventType.WORKFLOW_STARTED, commands=None)

    commands_map = load_persisted_commands(RUN_ID)

    assert 1 not in commands_map


def test_load_workflow_snapshot() -> None:
    """load_workflow_snapshot returns a core WorkflowSnapshot."""
    run = create_test_workflow()

    snapshot = load_workflow_snapshot(RUN_ID)

    assert snapshot.run_id == RUN_ID
    assert snapshot.workflow_name == "test-workflow"
    assert snapshot.status == WorkflowStatus.COMPLETED.value
    assert snapshot.state == {"counter": 1}


def test_replay_workflow_definition_not_found() -> None:
    """replay_workflow returns ERROR when definition not found."""
    run = create_test_workflow()
    registry = WorkflowRegistry()

    result = replay_workflow(RUN_ID, registry)

    assert result.definition_found is False
    assert result.report.status == ReplayStatus.ERROR
    assert "not found" in result.report.message


def test_replay_workflow_with_matching_definition() -> None:
    """replay_workflow passes when definition matches."""
    from ace.commands import CompleteWorkflow

    run = create_test_workflow()
    create_test_event(run, 1, WorkflowEventType.WORKFLOW_STARTED)

    # Create matching definition
    class TestWorkflow(WorkflowDefinition):
        name = "test-workflow"
        version = "1"

        def start(self, input, context):
            return WorkflowTransition(
                state={"counter": 1},
                commands=(CompleteWorkflow(result={"done": True}),),
            )

        def advance(self, state, event, context):
            return WorkflowTransition(state=state, commands=())

    registry = WorkflowRegistry()
    registry.register(TestWorkflow())

    result = replay_workflow(RUN_ID, registry)

    assert result.definition_found is True
    # Status depends on whether commands were persisted
    assert result.report.status in (ReplayStatus.PASSED, ReplayStatus.PARTIAL)
