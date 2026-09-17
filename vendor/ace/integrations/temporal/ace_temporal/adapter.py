"""Experimental Temporal execution adapter for ACE workflows.

The adapter is the sole execution authority for the runs it creates. It owns
its run IDs (``temporal-<hex>``), stamps authority metadata, and rejects run
IDs created by any other backend. The Temporal SDK is imported lazily so the
translation and validation logic works without it installed.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from ace.backend import TEMPORAL_CAPABILITIES, BackendName
from ace.engine import WorkflowEngine
from ace.exceptions import AceError, ConcurrentTransition, DefinitionNotFound, RunNotFound
from ace.models import WorkflowEvent, WorkflowEventType
from ace.registry import ActivityRegistry

from ace_temporal.codec import translate_commands

if TYPE_CHECKING:
    from ace.backend import BackendCapabilities
    from ace.commands import WorkflowCommand
    from ace.json_types import JsonObject, JsonValue
    from ace.models import WorkflowSnapshot, WorkflowStatus
    from ace.registry import WorkflowRegistry

    from ace_temporal.codec import TemporalCommandPlan


class ForeignRunError(AceError):
    """A run ID belongs to a different execution authority."""


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _TemporalIdGenerator:
    def new_id(self) -> str:
        return f"temporal-{uuid4().hex}"


class _TemporalRunStore:
    """In-memory stand-in for Temporal's durable persistence.

    In production, Temporal durably persists run state as server-side workflow
    histories. This store models that persistence as a dict of snapshots plus
    per-run event logs so the adapter's translation, validation, and authority
    logic can be exercised without a Temporal cluster. Commands are translated
    (and thereby validated against capabilities) before anything is persisted.
    """

    def __init__(self, capabilities: BackendCapabilities) -> None:
        self._capabilities = capabilities
        self._snapshots: dict[str, WorkflowSnapshot] = {}
        self._events: dict[str, list[WorkflowEvent]] = {}
        self._plans: dict[str, list[TemporalCommandPlan]] = {}
        self._metadata: dict[str, dict[str, str]] = {}

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
        self._snapshots[snapshot.run_id] = deepcopy(snapshot)
        self._events[snapshot.run_id] = [deepcopy(event)]
        self._plans[snapshot.run_id] = list(plans)
        self._metadata[snapshot.run_id] = {"execution_backend": "temporal"}
        return deepcopy(snapshot)

    def load(self, run_id: str) -> WorkflowSnapshot:
        try:
            return deepcopy(self._snapshots[run_id])
        except KeyError as exc:
            raise RunNotFound(f"Workflow run {run_id!r} was not found.") from exc

    def commit(
        self,
        expected_sequence: int,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
    ) -> WorkflowSnapshot:
        current = self.load(snapshot.run_id)
        if current.last_event_sequence != expected_sequence:
            raise ConcurrentTransition(
                f"Workflow run {snapshot.run_id!r} expected sequence {expected_sequence}, "
                f"but the current sequence is {current.last_event_sequence}."
            )
        plans = translate_commands(commands, self._capabilities)
        self._snapshots[snapshot.run_id] = deepcopy(snapshot)
        self._events[snapshot.run_id].append(deepcopy(event))
        self._plans[snapshot.run_id].extend(plans)
        return deepcopy(snapshot)

    def owns(self, run_id: str) -> bool:
        return run_id in self._snapshots

    def events(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        return tuple(deepcopy(self._events[run_id]))

    def plans(self, run_id: str) -> tuple[TemporalCommandPlan, ...]:
        return tuple(self._plans[run_id])

    def metadata(self, run_id: str) -> dict[str, str]:
        return dict(self._metadata[run_id])


class AceTemporalAdapter:
    """Translates ACE workflow transitions into Temporal-native actions."""

    capabilities: BackendCapabilities = TEMPORAL_CAPABILITIES
    backend_name: BackendName = BackendName.TEMPORAL

    def __init__(
        self,
        workflows: WorkflowRegistry,
        activities: ActivityRegistry | None = None,
    ) -> None:
        self._workflows = workflows
        self._activities = activities if activities is not None else ActivityRegistry()
        self._store = _TemporalRunStore(self.capabilities)
        self._engine = WorkflowEngine(
            workflows,
            self._store,
            _UtcClock(),
            _TemporalIdGenerator(),
        )
        self._seen_dedup_keys: dict[str, set[str]] = {}

    def create_workflow_run(
        self,
        workflow_name: str,
        workflow_version: str,
        input: JsonObject,
    ) -> str:
        """Start a run, validate its commands, persist the snapshot, return the run ID."""
        snapshot = self._engine.start(workflow_name, input, version=workflow_version)
        self._seen_dedup_keys[snapshot.run_id] = set()
        return snapshot.run_id

    def handle_event(
        self,
        run_id: str,
        event_type: str,
        payload: JsonObject | None = None,
        actor: str | None = None,
        *,
        dedup_key: str | None = None,
    ) -> WorkflowSnapshot:
        """Apply an event with adapter-supplied ID, sequence, and timestamp.

        Events carrying a dedup key (explicit, or ``payload["signal_id"]`` for
        signals) are applied at most once; duplicates return the current
        snapshot unchanged.
        """
        current = self._load_owned(run_id)
        key = dedup_key
        if key is None and event_type == WorkflowEventType.SIGNAL_RECEIVED:
            signal_id = (payload or {}).get("signal_id")
            if isinstance(signal_id, str):
                key = f"signal:{signal_id}"
        seen = self._seen_dedup_keys.setdefault(run_id, set())
        if key is not None and key in seen:
            return current
        event = WorkflowEvent(
            event_id=f"temporal-{uuid4().hex}",
            run_id=run_id,
            sequence=current.last_event_sequence + 1,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            payload=deepcopy(payload or {}),
            actor=actor,
        )
        updated, commands = self._engine.apply_event(current, event)
        persisted = self._store.commit(current.last_event_sequence, updated, event, commands)
        if key is not None:
            seen.add(key)
        return persisted

    def complete_activity(
        self,
        run_id: str,
        activity_key: str,
        result: JsonValue,
    ) -> None:
        self.handle_event(
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
    ) -> None:
        self.handle_event(
            run_id,
            WorkflowEventType.ACTIVITY_FAILED,
            payload={"activity_key": activity_key, "error_type": error_type, "message": message},
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
        return self._load_owned(run_id).status

    def get_snapshot(self, run_id: str) -> WorkflowSnapshot:
        return self._load_owned(run_id)

    def events(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        self._require_owned(run_id)
        return self._store.events(run_id)

    def command_plans(self, run_id: str) -> tuple[TemporalCommandPlan, ...]:
        self._require_owned(run_id)
        return self._store.plans(run_id)

    def run_metadata(self, run_id: str) -> dict[str, str]:
        self._require_owned(run_id)
        return self._store.metadata(run_id)

    def _require_owned(self, run_id: str) -> None:
        if not self._store.owns(run_id):
            raise ForeignRunError(
                f"Run {run_id!r} was not created by this Temporal adapter; "
                "each ACE run has exactly one execution authority."
            )

    def _load_owned(self, run_id: str) -> WorkflowSnapshot:
        self._require_owned(run_id)
        return self._store.load(run_id)


def build_temporal_workflow(
    adapter: AceTemporalAdapter,
    workflow_name: str,
    workflow_version: str,
) -> type:
    """Build a Temporal workflow class wrapping a registered ACE definition.

    Lazily imports the Temporal SDK; the returned class delegates each run to
    the adapter, which owns run IDs and command translation.
    """
    from temporalio import workflow

    if not adapter._workflows.has(workflow_name, workflow_version):
        raise DefinitionNotFound(
            f"Workflow {workflow_name!r} version {workflow_version!r} is not registered."
        )

    @workflow.defn(name=f"ace.{workflow_name}.v{workflow_version}", sandboxed=False)
    class AceTemporalWorkflow:
        @workflow.run
        async def run(self, input: JsonObject) -> str:
            return adapter.create_workflow_run(workflow_name, workflow_version, input)

    return AceTemporalWorkflow
