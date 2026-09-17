"""Workflow replay verification for determinism checking.

The replay verifier re-executes a workflow definition against persisted history
and compares the results. This detects nondeterministic workflow code that would
produce different commands or state on replay.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ace.codec import serialize_commands
from ace.engine import _validate_transition
from ace.models import WorkflowContext, WorkflowEventType, WorkflowSnapshot, WorkflowStatus

if TYPE_CHECKING:
    from ace.commands import WorkflowCommand
    from ace.definitions import WorkflowDefinition
    from ace.json_types import JsonObject
    from ace.models import WorkflowEvent


class ReplayStatus(StrEnum):
    """Result status of a replay verification."""

    # Replay matched persisted state and commands exactly.
    PASSED = "PASSED"
    # Replay verified state but commands were not persisted (legacy events).
    PARTIAL = "PARTIAL"
    # Replay produced different state or commands than persisted.
    FAILED = "FAILED"
    # Replay could not complete due to an error.
    ERROR = "ERROR"


@dataclass(frozen=True)
class EventReplayResult:
    """Result of replaying a single event."""

    sequence: int
    event_type: str
    status: ReplayStatus
    commands_available: bool
    message: str | None = None


@dataclass(frozen=True)
class ReplayReport:
    """Complete report of replaying a workflow run."""

    run_id: str
    status: ReplayStatus
    expected_status: WorkflowStatus
    replayed_status: WorkflowStatus | None
    expected_state: JsonObject
    replayed_state: JsonObject | None
    expected_result: object
    replayed_result: object
    expected_failure: object
    replayed_failure: object
    events: tuple[EventReplayResult, ...]
    message: str | None = None


class WorkflowReplayVerifier:
    """Verifies workflow determinism by replaying against history."""

    def replay(
        self,
        definition: WorkflowDefinition,
        snapshot: WorkflowSnapshot,
        events: tuple[WorkflowEvent, ...],
        persisted_commands: dict[int, tuple[WorkflowCommand, ...]] | None = None,
    ) -> ReplayReport:
        """Replay a workflow and compare against the persisted snapshot.

        Args:
            definition: The workflow definition to use for replay.
            snapshot: The expected final state from persistence.
            events: The complete event history in sequence order.
            persisted_commands: Optional map of sequence -> commands for verification.
                If None or missing for an event, command comparison is skipped (PARTIAL).

        Returns:
            A ReplayReport with status and per-event results.
        """
        if not events:
            return ReplayReport(
                run_id=snapshot.run_id,
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
                message="No events to replay.",
            )

        persisted_commands = persisted_commands or {}
        event_results: list[EventReplayResult] = []
        current_state: JsonObject = {}
        current_status = WorkflowStatus.PENDING
        current_result = None
        current_failure = None
        overall_status = ReplayStatus.PASSED

        for event in events:
            try:
                result = self._replay_event(
                    definition,
                    snapshot,
                    event,
                    current_state,
                    persisted_commands.get(event.sequence),
                )
                event_results.append(result)
                if result.status == ReplayStatus.FAILED:
                    overall_status = ReplayStatus.FAILED
                elif (
                    result.status == ReplayStatus.PARTIAL and overall_status == ReplayStatus.PASSED
                ):
                    overall_status = ReplayStatus.PARTIAL
            except Exception as exc:
                event_results.append(
                    EventReplayResult(
                        sequence=event.sequence,
                        event_type=event.event_type,
                        status=ReplayStatus.ERROR,
                        commands_available=event.sequence in persisted_commands,
                        message=str(exc),
                    )
                )
                overall_status = ReplayStatus.ERROR
                break

            # Update state from replay
            if event.event_type == WorkflowEventType.WORKFLOW_STARTED:
                context = WorkflowContext(run_id=snapshot.run_id, now=event.occurred_at)
                transition = definition.start(deepcopy(snapshot.input), context)
                current_state = deepcopy(transition.state)
                current_status, current_result, current_failure = _validate_transition(transition)
            else:
                context = WorkflowContext(run_id=snapshot.run_id, now=event.occurred_at)
                transition = definition.advance(deepcopy(current_state), deepcopy(event), context)
                current_state = deepcopy(transition.state)
                current_status, current_result, current_failure = _validate_transition(transition)

        # Final comparison
        if current_state != snapshot.state:
            overall_status = ReplayStatus.FAILED
        if current_status != snapshot.status:
            overall_status = ReplayStatus.FAILED
        if current_result != snapshot.result:
            overall_status = ReplayStatus.FAILED

        return ReplayReport(
            run_id=snapshot.run_id,
            status=overall_status,
            expected_status=snapshot.status,
            replayed_status=current_status,
            expected_state=snapshot.state,
            replayed_state=current_state,
            expected_result=snapshot.result,
            replayed_result=current_result,
            expected_failure=snapshot.failure,
            replayed_failure=current_failure,
            events=tuple(event_results),
            message=None if overall_status == ReplayStatus.PASSED else "Replay mismatch detected.",
        )

    def _replay_event(
        self,
        definition: WorkflowDefinition,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        current_state: JsonObject,
        expected_commands: tuple[WorkflowCommand, ...] | None,
    ) -> EventReplayResult:
        """Replay a single event and compare commands."""
        context = WorkflowContext(run_id=snapshot.run_id, now=event.occurred_at)

        if event.event_type == WorkflowEventType.WORKFLOW_STARTED:
            transition = definition.start(deepcopy(snapshot.input), context)
        else:
            transition = definition.advance(deepcopy(current_state), deepcopy(event), context)

        if expected_commands is None:
            # Legacy event without persisted commands - partial verification only
            return EventReplayResult(
                sequence=event.sequence,
                event_type=event.event_type,
                status=ReplayStatus.PARTIAL,
                commands_available=False,
                message="Commands not persisted for this event.",
            )

        # Compare serialized commands for deterministic comparison
        replayed_serialized = serialize_commands(transition.commands)
        expected_serialized = serialize_commands(expected_commands)

        if replayed_serialized != expected_serialized:
            return EventReplayResult(
                sequence=event.sequence,
                event_type=event.event_type,
                status=ReplayStatus.FAILED,
                commands_available=True,
                message="Commands differ from persisted history.",
            )

        return EventReplayResult(
            sequence=event.sequence,
            event_type=event.event_type,
            status=ReplayStatus.PASSED,
            commands_available=True,
        )
