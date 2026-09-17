# ACE — Activity & Command Engine

A durable workflow engine for Python. Define workflows as pure functions
of state and event, run them reliably across many processes, and never
lose a step — even when workers crash, leases expire, or deploys roll
through.

```
WorkflowDefinition (yours, pure Python)
        │  state + commands
        ▼
Workers ─────────── claim → execute → complete/fail with leases; completion
        │           writes a durable WorkflowInboxEvent in the same
        │           transaction as the activity outcome (outbox pattern)
        ▼
WorkflowInboxEvent ─ ordered, durable inbox per run; timers, signals,
        │            deadlines, and cancellation all enter the same path
        ▼
Dispatcher ───────── one event at a time per run, in sequence order;
        │            applies WorkflowEngine transitions inside a savepoint
        ▼
Atomic commit ────── snapshot + history + commands + inbox ack, together
```

## Packages

| Package | Path | What it does |
|---|---|---|
| **ace-core** | [`core/`](core/) | Pure-Python engine. Zero dependencies, zero I/O. Defines workflows, transitions, commands, replay verification, backend capabilities, and an in-memory store for testing. |
| **ace-django** | [`django/`](django/) | Django + PostgreSQL adapter — the **production authority**. Durable inbox, dispatcher, timers, signals, deadlines, queue QoS, leased workers, replay, health checks. No Redis, no broker. |
| **ace-dbos** | [`integrations/dbos/`](integrations/dbos/) | **EXPERIMENTAL.** DBOS execution backend for ace-core definitions. Never shares a run with another backend. |
| **ace-temporal** | [`integrations/temporal/`](integrations/temporal/) | **EXPERIMENTAL.** Temporal execution backend for ace-core definitions. Never shares a run with another backend. |

## Quick start

```python
from ace import (
    CompleteWorkflow, ScheduleActivity, WorkflowEngine, WorkflowEventType,
    WorkflowRegistry, WorkflowTransition, InMemoryExecutionStore,
    SystemClock, UuidGenerator,
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
        if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:  # "activity.completed"
            return WorkflowTransition(
                state=state,
                commands=(CompleteWorkflow(result="done"),),
            )
        return WorkflowTransition(state=state, commands=())

registry = WorkflowRegistry()
registry.register(Greet())

engine = WorkflowEngine(
    registry, InMemoryExecutionStore(), SystemClock(), UuidGenerator(),
)
snapshot = engine.start("greet", {"who": "world"}, idempotency_key="greet-world")
```

The adapter executes scheduled activities and feeds `activity.completed` /
`activity.failed` events back into the engine — in production, through the
durable inbox and dispatcher rather than a direct call.

## Key properties

- **Pure definitions** — workflows are deterministic functions of
  `(state, event) → (state, commands)`, verifiable against persisted
  history with `WorkflowReplayVerifier`.
- **Idempotent starts** — repeated calls with the same idempotency key
  return the existing run; mismatched input raises `IdempotencyConflict`.
- **Activity groups** — fan out N activities under a completion policy.
  `ALL_SUCCESS` (default) emits a single group-level event as soon as all
  members resolve, fail-fast cancelling siblings on the first exhausted
  member. `WAIT_ALL` never cancels siblings; it waits for every member to
  reach a terminal status, then emits one event carrying both `results`
  and `failures` so the workflow decides whether to continue past a
  partial failure.
- **Durable inbox** — activity outcomes, timers, signals, deadlines, and
  cancellation are committed as inbox rows and dispatched in strict
  per-run sequence order. A crashed dispatcher loses nothing.
- **Deterministic failure handling** — transition failures are retried
  with exponential backoff (optionally jittered via
  `RetryPolicy.jitter_fraction` to avoid synchronized retry storms), then
  dead-lettered; the run moves to `BLOCKED` for operator recovery instead
  of looping forever.
- **Injected time and IDs** — the engine never calls `datetime.now()` or
  `uuid4()` directly, making every test deterministic by construction.
- **Terminal is terminal** — completed, failed, and cancelled runs cannot
  be resurrected.

## Delivery guarantees

Be precise about what ACE does and does not promise
(details in [`docs/delivery-guarantees.md`](docs/delivery-guarantees.md)):

- Standard activity **invocation is at-least-once** — a crashed worker's
  activity is reclaimed and re-executed.
- Attempt **completion and inbox acceptance are exactly-once in ACE
  persistence** — ownership-token lease fencing rejects stale completions,
  and `(run, source_type, source_key)` uniqueness deduplicates inbox rows.
- **Signals are idempotently accepted** — a caller idempotency key makes
  retries return the same receipt; changed payloads conflict.
- **Transactional activities** couple same-database ORM writes with
  activity completion atomically — but invocation is still at-least-once.
- **No guarantee makes arbitrary external side effects exactly-once.**

## Running in production (Django)

```bash
pip install -e ./core -e ./django

# Activity workers (N supervised single-slot processes)
python manage.py runaceworker --workers 4 --queues medium \
    --lease-seconds 300 --renewal-seconds 60

# Dispatcher, timer, deadline, and activity-timeout services
python manage.py runaceservices --roles dispatcher,timer,deadline,timeouts

# Deploy gating and monitoring
python manage.py checkacehealth --readiness --fail-on-unready
python manage.py checkacehealth --failure-metrics
```

Operational commands: `syncacequeues` provisions `QueueConfig` rows from
settings, `replayaceworkflow` verifies a run's determinism against
history, `retryaceinbox` / `resumeaceworkflow` recover dead-lettered
events and `BLOCKED` runs, and `purgeacehistory` deletes old terminal
runs safely. See [`docs/operations/django-runtime.md`](docs/operations/django-runtime.md).

## Backend capability matrix

`ace-django` is the production authority; the DBOS and Temporal adapters
are experimental and validate every command against declared capabilities
(`ace.backend`). Each run is owned by exactly one backend.

| Capability | Django | DBOS | Temporal |
|---|---|---|---|
| Core transitions, retries, cancellation | ✅ | ✅ | ✅ |
| Timers | ✅ | ✅ | ✅ |
| Durable signals | ✅ | ✅ | ✅ |
| Durable inbox / dispatcher | ✅ | ❌ | ❌ |
| Queue QoS (concurrency, rate, partitions) | ✅ | ❌ | ❌ |
| Activity priority / delay | ✅ | ❌ | ❌ |
| Transactional activities | ✅ | ❌ | ❌ |
| Schedule-to-close / start-to-close timeouts | ✅ | ✅ | ✅ |
| Heartbeat timeouts | ✅ | ❌ | ✅ |
| Workflow deadlines | ✅ | ❌ | ✅ |
| Replay verification | ✅ | ❌ | ❌ |
| BLOCKED / dead-letter queue | ✅ | ❌ | ❌ |

## Tests

```bash
# Core — pure in-memory, no database
cd core && pip install -e '.[test]' && pytest

# Django — SQLite for unit tests, Postgres for multiprocess tests
cd django && pip install -e '.[test]' && pytest
cd django && pytest -m postgres   # needs real PostgreSQL
```

All test suites run with sockets blocked. Deterministic by construction:
fixed clocks, fixed ID sequences, no network.

## Documentation

- [`core/README.md`](core/README.md) — engine internals, store protocol,
  module layout
- [`django/README.md`](django/README.md) — adapter architecture,
  correctness properties, lock ordering, health checks
- [`docs/architecture/durability.md`](docs/architecture/durability.md) —
  inbox/outbox design, dispatcher commit protocol, crash boundaries
- [`docs/delivery-guarantees.md`](docs/delivery-guarantees.md) — the
  exact delivery contract
- [`docs/operations/django-runtime.md`](docs/operations/django-runtime.md) —
  running workers, services, and operational commands
- [`docs/versioning.md`](docs/versioning.md) — workflow versioning and
  safe deployment
- [`docs/backends.md`](docs/backends.md) — backend authority and adapter
  isolation rules
- [`docs/upgrading-0.1-to-1.0.md`](docs/upgrading-0.1-to-1.0.md) —
  compatibility breaks and rollout order

## License

Proprietary.
