# ace-core

Pure-Python durable workflow contracts and transition engine. Zero
dependencies, zero I/O, zero Django. Everything the engine does is a
deterministic function of `(current snapshot, event) -> (new snapshot,
commands)`.

This package defines **what a workflow is**. It never decides *where* state
lives or *who* runs an activity — that is the job of an execution adapter
(see `ace_django/` for the PostgreSQL one).

## Design in one paragraph

A workflow is a plain Python object with `start(input, context)` and
`advance(state, event, context)` methods, each returning a
`WorkflowTransition`: the next `state` dict plus zero or more commands
(`ScheduleActivity`, `ScheduleActivityGroup`, `ScheduleTimer`,
`CompleteWorkflow`, `FailWorkflow`, `CancelWorkflow`). The `WorkflowEngine`
validates the transition, appends a `WorkflowEvent` to the run's history,
and asks the `ExecutionStore` to commit snapshot + event + commands
atomically. Because definitions are pure functions of state and event,
every run is replayable and every test is deterministic.

## Layout

| Module | Contents |
|---|---|
| `ace/engine.py` | `WorkflowEngine` — `start`, `handle_event`, `request_cancellation`, `apply_event` (the dispatcher-facing API: event ID and timestamp are supplied by the persistence layer), transition validation |
| `ace/commands.py` | Command dataclasses + `ActivityGroupCompletionPolicy` (`ALL_SUCCESS` fail-fast, `WAIT_ALL` settle-all), `ActivityExecutionMode` (`STANDARD` / `TRANSACTIONAL`), `ActivityTimeoutConfig` |
| `ace/models.py` | `WorkflowSnapshot`, `WorkflowEvent(Type)`, `WorkflowStatus` (incl. `BLOCKED`), `RetryPolicy` (with optional `jitter_fraction`), `WorkflowFailure`, contexts |
| `ace/codec.py` | Canonical command serialization: `serialize_command(s)`, `serialize_commands_json`, `deserialize_command(s)` |
| `ace/replay.py` | `WorkflowReplayVerifier`, `ReplayReport`, `ReplayStatus` — verify determinism against persisted history |
| `ace/backend.py` | `BackendName`, `BackendCapabilities` + `DJANGO_CAPABILITIES` / `DBOS_CAPABILITIES` / `TEMPORAL_CAPABILITIES` |
| `ace/definitions.py` | Protocols: `WorkflowDefinition`, `Clock`, `IdGenerator`, `ActivityCallable` |
| `ace/store.py` | `ExecutionStore` protocol + thread-safe `InMemoryExecutionStore` |
| `ace/registry.py` | `WorkflowRegistry`, `ActivityRegistry` — explicit registration keyed by `(name, version)`, with `identities()` / `has()` for version routing |
| `ace/runtime.py` | `SystemClock`, `UuidGenerator` (injected, never called implicitly) |
| `ace/exceptions.py` | Named exceptions: `InvalidTransition`, `IdempotencyConflict`, `ConcurrentTransition`, … |
| `ace/json_types.py` | `JsonValue` / `JsonObject` aliases — all state and payloads are JSON-shaped |
| `ace/testing/backend_contract.py` | `BackendContractTest` — shared contract tests every execution adapter must pass |

## Quick start

```python
from ace import (
    CompleteWorkflow, ScheduleActivity, WorkflowEngine, WorkflowRegistry,
    WorkflowTransition, InMemoryExecutionStore, SystemClock, UuidGenerator,
)

class Greet:
    name = "greet"
    version = "1"

    def start(self, input, context):
        return WorkflowTransition(
            state={"who": input["who"]},
            commands=(
                ScheduleActivity(activity_key="say", activity_name="say_hello", input=input),
            ),
        )

    def advance(self, state, event, context):
        if event.event_type == "activity.completed":  # WorkflowEventType.ACTIVITY_COMPLETED
            return WorkflowTransition(state=state, commands=(CompleteWorkflow(result="done"),))
        return WorkflowTransition(state=state, commands=())

registry = WorkflowRegistry()
registry.register(Greet())
engine = WorkflowEngine(registry, InMemoryExecutionStore(), SystemClock(), UuidGenerator())
snapshot = engine.start("greet", {"who": "world"}, idempotency_key="greet-world")
```

The adapter (not this package) executes the scheduled activity and feeds
the resulting `activity.completed` / `activity.failed` event back into the
engine — `engine.handle_event(...)` for in-memory callers, or
`engine.apply_event(...)` from a durable dispatcher.

## Key semantics

- **Idempotent starts** — `start(..., idempotency_key=...)` returns the
  existing run for a repeated key; a reused key with different input or
  version raises `IdempotencyConflict`.
- **Activity groups** — `ScheduleActivityGroup` fans out N members under a
  completion policy: `ALL_SUCCESS` (default) fails fast — the first
  permanent member failure cancels unclaimed siblings and emits
  `activity_group.failed` immediately. `WAIT_ALL` never cancels siblings on
  failure; it waits for every member to reach a terminal status and always
  emits a single `activity_group.completed` carrying both `results` and
  `failures`, so the workflow's own transition logic decides whether
  partial failure is acceptable ("continue on failure"). Cancellation is
  rejected while a group is `RUNNING` (the adapter cancels members first).
- **Retry jitter** — `RetryPolicy.jitter_fraction` (default `0.0`, opt-in)
  randomizes each computed delay by up to that fraction, so a population of
  activities that fail in lockstep (parallel group members, shared
  upstream dependency) don't retry in lockstep too.
- **Dispatcher-facing `apply_event`** — takes a pre-built `WorkflowEvent`
  (stable event ID and timestamp supplied by the adapter), validates the
  sequence, and returns `(snapshot, commands)` for atomic persistence.
  `workflow.deadline_exceeded` is handled by a built-in terminal
  transition without calling the definition.
- **Scheduling controls** — `ScheduleActivity` carries `priority`,
  `delay_seconds`, `partition_key`, `execution_mode`, and a `timeout`
  (`ActivityTimeoutConfig`: schedule-to-close, start-to-close, heartbeat)
  in addition to the existing queue/retry/idempotency fields. Adapters
  validate these against their `BackendCapabilities`.
- **`WorkflowStatus.BLOCKED`** — nonterminal but not claimable; adapters
  use it when a run needs operator intervention. New 1.0 event types:
  `activity.timed_out`, `workflow.deadline_exceeded`,
  `workflow.signal_received`.
- **Terminal is terminal** — transitions against `COMPLETED` / `FAILED` /
  `CANCELLED` runs raise `InvalidTransition`. No resurrection paths.
- **Injected time and ids** — the engine never calls `datetime.now()` or
  `uuid4()` directly; `Clock` and `IdGenerator` are constructor
  dependencies, which is what makes replay tests exact.
- **Optimistic concurrency** — stores enforce `expected_sequence` on
  commit; a lost race raises `ConcurrentTransition` and the caller retries
  from a fresh `load()`.

## Replay verification

`WorkflowReplayVerifier.replay(definition, snapshot, events,
persisted_commands)` re-runs `start` from the persisted input and each
event with its historical `occurred_at`, comparing final
status/state/result/failure and per-event commands. Events whose
`commands` were not persisted (legacy 0.1 rows) yield a `PARTIAL` report —
state verified, commands unavailable — never a false full pass.

## Backend portability

The `ExecutionStore` protocol (4 methods: `find_idempotent`, `start`,
`load`, `commit`) is the persistence seam; `InMemoryExecutionStore` ships
here and backs the engine test suite. `ace/backend.py` declares per-backend
capabilities, and `ace/testing/backend_contract.py` defines the contract
every adapter must pass. `ace-django` is the production authority; DBOS
and Temporal adapters are experimental — see
[`docs/backends.md`](../docs/backends.md).

## Tests

```bash
cd core && pip install -e '.[test]' && pytest   # all in-memory, sockets blocked
```

Deterministic by construction: fixed clocks, fixed id sequences, no
network, no database.
