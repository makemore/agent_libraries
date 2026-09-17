# ace-django

Django + PostgreSQL durable execution adapter for [`ace-core`](../core/) —
the **production authority** backend. Where `ace-core` decides *what
happens next*, this package makes it actually happen across many
processes, using PostgreSQL as the single transactional authority for
state, queueing, leasing, the durable inbox, dispatch, fan-in, retries,
timers, signals, deadlines, and health.

There is no Redis, no message broker, no sidecar. Every coordination
primitive is a Postgres row + `select_for_update`. If the database says an
attempt completed, it completed — and nothing else gets to disagree.

## Architecture

```
WorkflowDefinition (yours, pure)              ace-core
        │
DjangoWorkflowClient ──── start / signal / request_cancellation / get_snapshot
        │
DjangoActivityQueue ───── claim / renew / complete / fail / cancel with
        │                 leases and queue QoS; completion writes a
        │                 WorkflowInboxEvent in the same transaction
        ▼
WorkflowInboxEvent ────── ordered durable inbox — activity and group
        │                 outcomes, timers, signals, deadlines, and
        │                 cancellation all enter here
        ▼
DjangoWorkflowDispatcher ─ lowest pending sequence per run, no overtaking;
        │                  applies WorkflowEngine.apply_event in a savepoint
        │                  and commits snapshot + history + commands +
        │                  inbox ack atomically
        ▼
runaceworker / runaceservices ── supervised worker and service processes
```

| Module | Contents |
|---|---|
| `models.py` | `WorkflowRun` (incl. `execution_backend`, `BLOCKED` fields), `ActivityGroupRun`, `ActivityRun`, `ActivityAttempt`, `WorkflowEvent`, `WorkflowTimer`, `WorkflowInboxEvent`, `WorkflowTransitionFailure`, `QueueConfig`, `AceWorkerHeartbeat` |
| `store.py` | `DjangoExecutionStore` — implements the `ExecutionStore` protocol; commit maps commands to `ActivityRun` / `ActivityGroupRun` / `WorkflowTimer` rows in the same transaction |
| `queue.py` | `DjangoActivityQueue` — `SKIP LOCKED` claims with QoS (`QueueConfig` concurrency, fixed-window rate limits, partition concurrency, priority, delay), ownership-token leases, group fan-in, fail-fast sibling cancellation |
| `inbox.py` | `enqueue_locked` + inbox state transitions — `(run, source_type, source_key)` uniqueness makes delivery idempotent |
| `dispatcher.py` | `DjangoWorkflowDispatcher` — ordered dispatch, savepointed transitions, retry budget, `WorkflowTransitionFailure`, DLQ + `BLOCKED` |
| `client.py` | `DjangoWorkflowClient` — `start` (with optional `deadline_at`), `signal`, `request_cancellation`, `get_snapshot` |
| `timers.py` | `DjangoTimerService` — delivers due timers to the inbox, marks `DELIVERED` atomically, retries then dead-letters |
| `deadlines.py` | `DjangoDeadlineService` — emits `workflow.deadline_exceeded` for runs past `deadline_at` |
| `activity_timeouts.py` | `DjangoActivityTimeoutService` — schedule-to-close, start-to-close, and heartbeat timeout enforcement |
| `transactional.py` | `DjangoTransactionalActivityExecutor` — same-database ORM writes + completion committed atomically |
| `runtime.py` | `DjangoRuntime` (replaces deprecated `WorkerRuntime`) + `load_runtime` via `ACE_RUNTIME_FACTORY` |
| `replay.py` | Persisted loaders + `replay_workflow` for determinism verification |
| `worker.py` | Single-slot worker loop with a renewable-lease heartbeat thread |
| `supervisor.py` / `process.py` | Multi-process supervisor behind `runaceworker` |
| `health.py` | `collect_ace_readiness` (deploy gating) vs `collect_ace_failure_metrics` (alerting), queue readiness, group reconciliation |
| `heartbeats.py` | Role-tagged service heartbeats (`DISPATCHER`, `TIMER`, `DEADLINE`) with version capabilities |
| `management/commands/` | `runaceworker`, `runaceservices`, `checkacehealth`, `checkacereconciliation`, `syncacequeues`, `replayaceworkflow`, `retryaceinbox`, `resumeaceworkflow`, `purgeacehistory` |

## Correctness properties (and how they're enforced)

- **Exactly-once completion recording in ACE persistence via
  ownership-token fencing.** Each claim issues an ownership token; a
  worker whose lease expired and was reclaimed gets `LeaseOwnershipLost`
  on complete/fail — the row already belongs to someone else. Invocation
  itself is at-least-once (see [`../docs/delivery-guarantees.md`](../docs/delivery-guarantees.md)).
- **Outbox-in-same-transaction.** Activity and group outcomes commit
  together with exactly one pending `WorkflowInboxEvent`. A crash between
  completion and dispatch loses nothing; a duplicate source insertion
  returns the existing row only if identical.
- **No overtaking.** The dispatcher processes each run's inbox strictly in
  `inbox_sequence` order; the stable inbox UUID becomes the core event ID,
  so a retried dispatch cannot duplicate history.
- **Savepointed transitions.** `WorkflowEngine.apply_event` runs inside an
  inner savepoint; on success, snapshot + `WorkflowEvent` (with canonical
  serialized commands) + materialized command rows + inbox `PROCESSED`
  commit as one transaction. On failure the savepoint rolls back and the
  failure is recorded — a transition can never partially commit.
- **Deterministic recovery budget.** Transition failures retry with
  exponential backoff; after the budget is exhausted the event is
  `DEAD_LETTER`ed, a `WorkflowTransitionFailure` records the resolution,
  and the run moves to `BLOCKED` (nonterminal, not claimable).
- **Crash-safe claims.** `claim()` uses `SELECT ... FOR UPDATE SKIP LOCKED`
  on `READY`/expired-lease rows, so a dead worker's work is picked up by
  the next poll with no janitor process.
- **Transactional fan-in.** The final member of an `ActivityGroupRun`
  completes inside a transaction that locks the group and workflow rows,
  flips the group terminal, and enqueues one `activity_group.completed`
  inbox event — all-or-nothing. Fail-fast: an exhausted member fails the
  group and cancels claimable siblings in the same transaction.
- **Deadlock-free by lock ordering.** All multi-row transactions lock
  top-down: `QueueConfig` → `WorkflowRun` → `ActivityGroupRun` →
  `ActivityRun` → `ActivityAttempt` → `WorkflowTimer` →
  `WorkflowInboxEvent`. Never lock upward. Application rows used by
  transactional activities are locked only after all ACE rows.
- **Terminal workflow guards.** Late outcomes against a terminal workflow
  are recorded and the remaining inbox events are `DISCARDED` with a
  reason rather than resurrecting the run.

## Runtime configuration

Workers and services resolve their registries through the
`ACE_RUNTIME_FACTORY` setting — a dotted path to a callable returning a
`DjangoRuntime`:

```python
# settings.py
ACE_RUNTIME_FACTORY = "myproject.ace_runtime.build_runtime"

# myproject/ace_runtime.py
from ace_django.runtime import DjangoRuntime

def build_runtime() -> DjangoRuntime:
    return DjangoRuntime(
        activities=activity_registry,
        workflows=workflow_registry,   # required for the dispatcher role
        deployment_id="deploy-2026-08-18",
    )
```

`DjangoRuntime` replaces the deprecated `WorkerRuntime`: activity workers
no longer hold a workflow engine (the inbox handles transitions), and the
runtime advertises workflow/activity `(name, version)` capabilities in
heartbeats for version routing. Legacy `WorkerRuntime` factories are still
accepted and upgraded with empty capabilities.

Settings (legacy `SUBMISSION_ACE_*` names are still read as a fallback):

| Setting | Used by | Meaning |
|---|---|---|
| `ACE_RUNTIME_FACTORY` | workers, services | Dotted path to the runtime factory |
| `ACE_QUEUES` | `syncacequeues` | Dict of queue name → QoS config |
| `ACE_EXPECTED_QUEUES` | `checkacehealth` | Queues that must appear in the health report |
| `ACE_WORKERS_EXPECTED` | `checkacehealth` | Whether missing worker heartbeats are unhealthy |
| `ACE_HEARTBEAT_STALE_SECONDS` | `checkacehealth` | Heartbeat freshness cutoff (default 60) |
| `ACE_OLDEST_READY_SECONDS` | `checkacehealth` | Oldest-ready age threshold (default 300) |

## Version routing

Workflow and activity definitions are keyed by `(name, version)` and each
run pins its `workflow_version` at start:

- **Workers** claim only activities whose `(activity_name,
  activity_version)` they advertise; other rows stay `READY` for a capable
  worker.
- **The dispatcher** skips runs pinned to workflow versions it has not
  registered: their inbox events stay `PENDING` without spending recovery
  budget, and other runs are not starved.

Never edit behavior for a published `(name, version)` — deploy old + new
definitions side by side until old runs drain. See
[`../docs/versioning.md`](../docs/versioning.md).

## Running workers and services

```bash
# Activity workers: supervisor → N single-slot processes
python manage.py runaceworker --workers 4 --queues medium \
    --lease-seconds 300 --renewal-seconds 60

# Dispatcher, timer, deadline, and activity-timeout services
python manage.py runaceservices --roles dispatcher,timer,deadline,timeouts
```

Each worker process is single-slot: one activity at a time, with a
background thread renewing the lease so long-running activities survive.
Both commands drain gracefully on SIGINT/SIGTERM.

## Health, readiness, and recovery

```bash
python manage.py checkacehealth --readiness --fail-on-unready   # deploy gate
python manage.py checkacehealth --failure-metrics               # alerting counts
python manage.py checkacereconciliation --fail-on-inconsistency
```

Readiness (`collect_ace_readiness`) checks that the system can accept new
work — database, fresh dispatcher/timer/deadline heartbeats, queue
configs, worker coverage — and deliberately ignores historical failures.
Failure metrics (`collect_ace_failure_metrics`) count `BLOCKED` runs,
inbox/timer dead letters, transition failures, timeouts, and failed
runs/activities/groups for dashboards and alerts.

When a run is `BLOCKED` (transition retry budget exhausted), recover with:

```bash
python manage.py retryaceinbox <run_id> --dry-run   # inspect dead-lettered events
python manage.py resumeaceworkflow <run_id>         # restore status, reset DLQ events
```

`resumeaceworkflow` restores `blocked_from_status`, clears the blocking
metadata, and resets dead-lettered events to `RETRYING` in one
transaction. `retryaceinbox` handles dead-lettered events on runs that are
not blocked or terminal.

## Tests

```bash
cd django && pip install -e '.[test]' && pytest
cd django && pytest -m postgres   # multiprocess lease/QoS/dispatch races
```

SQLite covers models, migrations, inbox/dispatcher/timer/signal logic, and
replay deterministically; tests marked `postgres` need a real PostgreSQL
plus separate OS processes for `SKIP LOCKED` and contention semantics.

## What this package does NOT know about

Your domain. This package is reusable by any Django project that installs
`ace-core` and points `ACE_RUNTIME_FACTORY` at its own registries. It also
never imports the experimental DBOS/Temporal adapters — each run belongs
to exactly one backend (`WorkflowRun.execution_backend`).

## Further reading

- [`../docs/architecture/durability.md`](../docs/architecture/durability.md) —
  inbox/outbox design, dispatcher commit protocol, crash boundaries
- [`../docs/operations/django-runtime.md`](../docs/operations/django-runtime.md) —
  all management commands and flags
- [`../docs/delivery-guarantees.md`](../docs/delivery-guarantees.md) — the
  exact delivery contract
- [`../docs/upgrading-0.1-to-1.0.md`](../docs/upgrading-0.1-to-1.0.md) —
  compatibility breaks and rollout order
