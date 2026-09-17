"""Execution-store protocol and deterministic in-memory implementation."""

from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from ace.exceptions import ConcurrentTransition, IdempotencyConflict, RunNotFound

if TYPE_CHECKING:
    from ace.commands import WorkflowCommand
    from ace.models import WorkflowEvent, WorkflowSnapshot


class ExecutionStore(Protocol):
    def find_idempotent(
        self,
        namespace: str,
        workflow_name: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot | None: ...

    def start(
        self,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
        *,
        idempotency_key: str | None,
    ) -> WorkflowSnapshot: ...

    def load(self, run_id: str) -> WorkflowSnapshot: ...

    def commit(
        self,
        expected_sequence: int,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
    ) -> WorkflowSnapshot: ...


class InMemoryExecutionStore:
    """Thread-safe store for unit tests and synchronous local execution."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._snapshots: dict[str, WorkflowSnapshot] = {}
        self._events: dict[str, list[WorkflowEvent]] = {}
        self._commands: dict[str, list[WorkflowCommand]] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}

    def find_idempotent(
        self,
        namespace: str,
        workflow_name: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot | None:
        with self._lock:
            run_id = self._idempotency.get((namespace, workflow_name, idempotency_key))
            if run_id is None:
                return None
            return deepcopy(self._snapshots[run_id])

    def start(
        self,
        snapshot: WorkflowSnapshot,
        event: WorkflowEvent,
        commands: tuple[WorkflowCommand, ...],
        *,
        idempotency_key: str | None,
    ) -> WorkflowSnapshot:
        with self._lock:
            if idempotency_key is not None:
                key = (snapshot.namespace, snapshot.workflow_name, idempotency_key)
                existing_id = self._idempotency.get(key)
                if existing_id is not None:
                    existing = self._snapshots[existing_id]
                    if (
                        existing.workflow_version != snapshot.workflow_version
                        or existing.input != snapshot.input
                    ):
                        raise IdempotencyConflict(
                            f"Workflow idempotency key {idempotency_key!r} in namespace "
                            f"{snapshot.namespace!r} was reused with different input or version."
                        )
                    return deepcopy(existing)

            if snapshot.run_id in self._snapshots:
                raise IdempotencyConflict(f"Workflow run ID {snapshot.run_id!r} already exists.")

            self._snapshots[snapshot.run_id] = deepcopy(snapshot)
            self._events[snapshot.run_id] = [deepcopy(event)]
            self._commands[snapshot.run_id] = list(deepcopy(commands))
            if idempotency_key is not None:
                self._idempotency[(snapshot.namespace, snapshot.workflow_name, idempotency_key)] = (
                    snapshot.run_id
                )
            return deepcopy(snapshot)

    def load(self, run_id: str) -> WorkflowSnapshot:
        with self._lock:
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
        with self._lock:
            try:
                current = self._snapshots[snapshot.run_id]
            except KeyError as exc:
                raise RunNotFound(f"Workflow run {snapshot.run_id!r} was not found.") from exc
            if current.last_event_sequence != expected_sequence:
                raise ConcurrentTransition(
                    f"Workflow run {snapshot.run_id!r} expected sequence {expected_sequence}, "
                    f"but the current sequence is {current.last_event_sequence}."
                )
            self._snapshots[snapshot.run_id] = deepcopy(snapshot)
            self._events[snapshot.run_id].append(deepcopy(event))
            self._commands[snapshot.run_id].extend(deepcopy(commands))
            return deepcopy(snapshot)

    def history(self, run_id: str) -> tuple[WorkflowEvent, ...]:
        with self._lock:
            if run_id not in self._events:
                raise RunNotFound(f"Workflow run {run_id!r} was not found.")
            return tuple(deepcopy(self._events[run_id]))

    def commands(self, run_id: str) -> tuple[WorkflowCommand, ...]:
        with self._lock:
            if run_id not in self._commands:
                raise RunNotFound(f"Workflow run {run_id!r} was not found.")
            return tuple(deepcopy(self._commands[run_id]))
