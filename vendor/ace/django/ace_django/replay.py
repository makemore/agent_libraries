"""Django-specific replay verification for workflow determinism.

Provides Django ORM loaders for workflow history and integration with the
core replay verifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ace.codec import deserialize_commands
from ace.models import WorkflowEvent, WorkflowSnapshot
from ace.replay import ReplayReport, ReplayStatus, WorkflowReplayVerifier
from ace_django.models import WorkflowEvent as DjangoWorkflowEvent, WorkflowRun

if TYPE_CHECKING:
    from uuid import UUID

    from ace.definitions import WorkflowDefinition
    from ace.registry import WorkflowRegistry


@dataclass(frozen=True)
class DjangoReplayResult:
    """Result of replaying a workflow run with Django loaders."""

    run_id: str
    workflow_name: str
    workflow_version: str
    report: ReplayReport
    definition_found: bool
    events_loaded: int
    commands_loaded: int


def load_workflow_events(run_id: str | UUID) -> tuple[WorkflowEvent, ...]:
    """Load workflow events from Django ORM.

    Returns core WorkflowEvent dataclasses from persisted DjangoWorkflowEvent models.
    """
    django_events = DjangoWorkflowEvent.objects.filter(workflow_run_id=run_id).order_by("sequence")

    return tuple(
        WorkflowEvent(
            event_id=str(e.pk),
            run_id=str(run_id),
            sequence=e.sequence,
            event_type=e.event_type,
            payload=e.payload,
            actor=e.actor,
            occurred_at=e.occurred_at,
        )
        for e in django_events
    )


def load_persisted_commands(run_id: str | UUID) -> dict[int, tuple]:
    """Load persisted commands from workflow events.

    Returns a dict mapping sequence number to deserialized commands.
    Events without commands (legacy or null) are omitted from the dict.
    """
    django_events = DjangoWorkflowEvent.objects.filter(workflow_run_id=run_id).order_by("sequence")

    commands_map = {}
    for e in django_events:
        if e.commands is not None:
            try:
                commands = deserialize_commands(e.commands)
                commands_map[e.sequence] = commands
            except Exception:
                # Skip events with invalid command serialization
                pass

    return commands_map


def load_workflow_snapshot(run_id: str | UUID) -> WorkflowSnapshot:
    """Load a workflow snapshot from Django ORM."""
    from ace.models import WorkflowFailure, WorkflowStatus as CoreWorkflowStatus

    run = WorkflowRun.objects.get(pk=run_id)

    # Convert failure dict to WorkflowFailure if present
    failure = None
    if run.failure:
        failure = WorkflowFailure(
            error_type=run.failure.get("error_type", ""),
            message=run.failure.get("message", ""),
            details=run.failure.get("details", {}),
        )

    return WorkflowSnapshot(
        run_id=str(run.pk),
        namespace=run.namespace,
        workflow_name=run.workflow_name,
        workflow_version=run.workflow_version,
        status=CoreWorkflowStatus(run.status),
        input=run.input,
        state=run.state,
        result=run.result,
        failure=failure,
        last_event_sequence=run.last_event_sequence,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def replay_workflow(
    run_id: str | UUID,
    registry: WorkflowRegistry,
) -> DjangoReplayResult:
    """Replay a workflow run against its persisted history.

    Args:
        run_id: The workflow run ID to replay.
        registry: The workflow registry to find definitions.

    Returns:
        A DjangoReplayResult with the replay report and loading metadata.
    """
    # Load workflow snapshot
    snapshot = load_workflow_snapshot(run_id)

    # Find definition
    try:
        definition = registry.resolve(snapshot.workflow_name, snapshot.workflow_version)
        definition_found = True
    except Exception:
        definition = None
        definition_found = False

    if not definition_found:
        return DjangoReplayResult(
            run_id=str(run_id),
            workflow_name=snapshot.workflow_name,
            workflow_version=snapshot.workflow_version,
            report=ReplayReport(
                run_id=str(run_id),
                status=ReplayStatus.ERROR,
                expected_status=snapshot.status,
                replayed_status=None,
                expected_state=snapshot.state,
                replayed_state=None,
                expected_result=snapshot.result,
                replayed_result=None,
                expected_failure=snapshot.failure,
                replayed_failure=None,
                events=(),
                message=f"Workflow definition not found: {snapshot.workflow_name}:{snapshot.workflow_version}",
            ),
            definition_found=False,
            events_loaded=0,
            commands_loaded=0,
        )

    # Load events and commands
    events = load_workflow_events(run_id)
    persisted_commands = load_persisted_commands(run_id)

    # Run replay
    verifier = WorkflowReplayVerifier()
    report = verifier.replay(definition, snapshot, events, persisted_commands)

    return DjangoReplayResult(
        run_id=str(run_id),
        workflow_name=snapshot.workflow_name,
        workflow_version=snapshot.workflow_version,
        report=report,
        definition_found=True,
        events_loaded=len(events),
        commands_loaded=len(persisted_commands),
    )
