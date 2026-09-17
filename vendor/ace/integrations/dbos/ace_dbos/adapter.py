"""DBOS backend adapter for ACE workflow runs.

The adapter is the sole authority for the runs it creates: it generates
"dbos-" prefixed run IDs, stamps authority metadata, and rejects run IDs
owned by other backends. Run state is kept in an in-memory store keyed by
run ID; in production DBOS durably persists this state in its
Postgres-backed system database. All translation and validation logic is
SDK-independent — the DBOS SDK is imported lazily and only by
`build_dbos_workflow`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import uuid4

from ace.backend import DBOS_CAPABILITIES, BackendName
from ace.engine import WorkflowEngine
from ace.exceptions import AceError, ConcurrentTransition, RunNotFound
from ace.models import WorkflowEventType
from ace.runtime import SystemClock

from ace_dbos.codec import translate_commands

if TYPE_CHECKING:
    from collections.abc import Callable

    from ace.backend import BackendCapabilities
    from ace.commands import WorkflowCommand
    from ace.json_types import JsonObject, JsonValue
    from ace.models import WorkflowEvent, WorkflowSnapshot, WorkflowStatus
    from ace.registry import ActivityRegistry, WorkflowRegistry

    from ace_dbos.codec import DbosCommandPlan


class ForeignRunError(AceError):
    """A run ID belongs to another authority (Django, Temporal, or unknown)."""


class _DbosIdGenerator:
    def new_id(self) -> str:
        return f"dbos-{uuid4().hex}"


@dataclass
class _RunRecord:
    snapshot: WorkflowSnapshot
    events: list[WorkflowEvent]
    plans: list[DbosCommandPlan]
    authority: JsonObject
    seen_source_keys: set[str] = field(default_factory=set)


class _MemoryAuthorityStore:
    """In-memory stand-in for the run state DBOS durably persists.

    Production deployments rely on DBOS's Postgres-backed durability; this
    store keeps the adapter fully testable without the SDK installed.
    Commands are validated against backend capabilities before persisting.
    """

    def __init__(self, capabilities: BackendCapabilities) -> None:
        self._capabilities = capabilities
        self.records: dict[str, _RunRecord] = {}

    def find_idempotent(
        self,
        namespace: str,
        workflow_name: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot | None:
        return None

    def start(
        self,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
        *,
        idempotency_key: str | None,
    ) -> WorkflowSnapshot:
        plans = translate_commands(commands, self._capabilities)
        self.records[snapshot.run_id] = _RunRecord(
            snapshot=snapshot,
            events=[event],
            plans=list(plans),
            authority={"execution_backend": "dbos"},
        )
        return snapshot

    def load(self, run_id: str) -> WorkflowSnapshot:
        record = self.records.get(run_id)
        if record is None:
            raise RunNotFound(f"Workflow run {run_id!r} does not exist.")
        return record.snapshot

    def commit(
        self,
        expected_sequence: int,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
    ) -> WorkflowSnapshot:
        record = self.records[snapshot.run_id]
        if record.snapshot.last_event_sequence != expected_sequence:
            raise ConcurrentTransition(f"Workflow run {snapshot.run_id!r} changed concurrently.")
        plans = translate_commands(commands, self._capabilities)
        record.snapshot = snapshot
        record.events.append(event)
        record.plans.extend(plans)
        return snapshot


class AceDbosAdapter:
    """Runs ACE workflows under DBOS authority.

    Never share a run with the Django or Temporal backends: each run has
    exactly one authority, and this adapter refuses foreign run IDs.
    """

    capabilities = DBOS_CAPABILITIES
    backend_name = BackendName.DBOS

    def __init__(
        self,
        workflows: WorkflowRegistry,
        activities: ActivityRegistry | None = None,
    ) -> None:
        self._workflows = workflows
        self._activities = activities
        self._store = _MemoryAuthorityStore(self.capabilities)
        self._engine = WorkflowEngine(
            workflows,
            self._store,
            SystemClock(),
            _DbosIdGenerator(),
        )

    def create_workflow_run(
        self,
        workflow_name: str,
        workflow_version: str,
        input: JsonObject,
    ) -> str:
        """Start a run, validating every emitted command against capabilities."""
        snapshot = self._engine.start(workflow_name, input, version=workflow_version)
        return snapshot.run_id

    def handle_event(
        self,
        run_id: str,
        event_type: str,
        payload: JsonObject | None = None,
        *,
        actor: str | None = None,
        source_key: str | None = None,
    ) -> WorkflowSnapshot:
        """Apply an event to an owned run.

        The event ID, sequence, and timestamp are adapter-supplied. Events
        carrying a `source_key` are deduplicated: redelivery is a no-op.
        """
        record = self._require_owned(run_id)
        if source_key is not None and source_key in record.seen_source_keys:
            return record.snapshot
        snapshot = self._engine.handle_event(
            run_id,
            event_type,
            payload=payload,
            actor=actor,
        )
        if source_key is not None:
            record.seen_source_keys.add(source_key)
        return snapshot

    def complete_activity(
        self,
        run_id: str,
        activity_key: str,
        result: JsonValue,
    ) -> WorkflowSnapshot:
        return self.handle_event(
            run_id,
            WorkflowEventType.ACTIVITY_COMPLETED,
            payload={"activity_key": activity_key, "result": result},
        )

    def fail_activity(
        self,
        run_id: str,
        activity_key: str,
        error_type: str,
        message: str,
    ) -> WorkflowSnapshot:
        return self.handle_event(
            run_id,
            WorkflowEventType.ACTIVITY_FAILED,
            payload={
                "activity_key": activity_key,
                "error_type": error_type,
                "message": message,
            },
        )

    def request_cancellation(
        self,
        run_id: str,
        *,
        reason: str,
        actor: str | None = None,
    ) -> WorkflowSnapshot:
        return self.handle_event(
            run_id,
            WorkflowEventType.CANCELLATION_REQUESTED,
            payload={"reason": reason},
            actor=actor,
        )

    def get_workflow_status(self, run_id: str) -> WorkflowStatus:
        return self._require_owned(run_id).snapshot.status

    def get_snapshot(self, run_id: str) -> WorkflowSnapshot:
        return self._require_owned(run_id).snapshot

    def get_authority(self, run_id: str) -> JsonObject:
        return dict(self._require_owned(run_id).authority)

    def list_events(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        return tuple(self._require_owned(run_id).events)

    def list_plans(self, run_id: str) -> tuple[DbosCommandPlan, ...]:
        return tuple(self._require_owned(run_id).plans)

    def _require_owned(self, run_id: str) -> _RunRecord:
        record = self._store.records.get(run_id)
        if record is None:
            raise ForeignRunError(
                f"Run {run_id!r} was not created by this DBOS adapter; "
                "refusing to act on a run owned by another authority."
            )
        return record


def build_dbos_workflow(
    adapter: AceDbosAdapter,
    workflow_name: str,
    workflow_version: str,
) -> Callable[[JsonObject], str]:
    """Wrap an adapter-managed workflow as a DBOS workflow callable.

    Imports the DBOS SDK lazily so the adapter stays usable without it.
    """
    from dbos import DBOS

    def _run(input: JsonObject) -> str:
        return adapter.create_workflow_run(workflow_name, workflow_version, input)

    _run.__name__ = f"ace_{workflow_name}_v{workflow_version}"
    _run.__qualname__ = _run.__name__
    return DBOS.workflow()(_run)
