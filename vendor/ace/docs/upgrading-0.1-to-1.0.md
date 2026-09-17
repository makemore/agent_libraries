# Upgrading from 0.1 to 1.0

1.0 is a breaking release: activity completion moves from in-transaction
engine calls to a durable inbox processed by a dispatcher. Read the whole
document before starting; the rollout order is not optional.

## Compatibility breaks

| 0.1 | 1.0 |
|---|---|
| `WorkerRuntime(activities, workflow_engine)` | `DjangoRuntime(activities, workflows=..., deployment_id=...)` — workers no longer hold an engine; legacy factories are upgraded with empty capabilities |
| `DjangoActivityQueue` takes an engine, `complete/fail/cancel` return snapshots | Queue never calls the engine; outcomes enqueue a `WorkflowInboxEvent` and return nothing |
| Workflow transitions visible when the worker's transaction commits | Transitions are **asynchronously visible** after the dispatcher processes the inbox event |
| Callers use `engine.handle_event` / `engine.request_cancellation` post-start | Callers use `DjangoWorkflowClient` (`start`, `signal`, `request_cancellation`, `get_snapshot`) |
| Timer status `FIRED` | `DELIVERED` (migration maps existing rows) |
| No inbox, no `BLOCKED` | New `WorkflowInboxEvent` states and nonterminal `BLOCKED` run status |
| Queues implicit | Queue must exist as an enabled `QueueConfig` row (`syncacequeues`) |
| `SUBMISSION_ACE_*` settings | `ACE_*` settings (legacy names still read as fallback) |
| No dispatcher/timer/deadline processes | `runaceservices` is required — without it, runs never advance |

A focused shim keeps legacy rows readable (old `WorkflowEvent` rows have
`commands=NULL` and replay as `PARTIAL`), but there are no dual completion
paths: **0.1 workers must never run after 1.0 services start** — they
bypass the inbox and would commit transitions the dispatcher does not
know about.

## Migrations

Three additive migrations: `0003_durable_inbox_and_runtime_fields`
(nullable fields and new tables), `0004_backfill_v01_runtime` (data), and
`0005_durable_constraints_and_indexes`. The backfill:

- stamps every existing run `execution_backend="django"`,
  `last_inbox_sequence=0`;
- creates enabled `QueueConfig` rows for every distinct existing queue
  plus `medium`;
- maps `FIRED` timers to `DELIVERED`;
- leaves old event `commands` NULL and deadlines/partitions/timeouts
  unset;
- does **not** synthesize inbox rows for already-terminal outcomes (0.1
  already committed their transitions); existing `SCHEDULED` timers become
  eligible once the timer service starts.

Assess table size before `0005` and schedule a PostgreSQL maintenance
window for index builds on large tables.

## Rollout order

1. **Backup/restore drill.** Verify you can restore the pre-upgrade
   database; after any 1.0 write, this backup is the only rollback.
2. **Deploy 1.0 code with services disabled.** The code reads old rows;
   nothing new runs yet.
3. **Stop all 0.1 workers.** They must not run past this point.
4. **Apply migrations 0003–0005.**
5. **Provision and verify:** `syncacequeues`, then
   `checkacehealth --readiness`, then `replayaceworkflow` on a sample of
   terminal runs (expect `PASSED` or `PARTIAL`, never `FAILED`).
6. **Start `runaceservices`** with all old workflow versions registered in
   the runtime factory.
7. **Deploy 1.0 activity workers** (`runaceworker`).
8. **Enable new features:** signals, timers, deadlines, queue QoS,
   activity timeouts.
9. **Canary transactional mode** on one low-risk activity before wider
   adoption.
10. **Enable experimental adapters** (if at all) only for newly created,
    explicitly selected runs — see [backends.md](backends.md).
11. **Monitor:** inbox event age, `BLOCKED`/DLQ counts
    (`checkacehealth --failure-metrics`), timer lag, version coverage.
12. **Retire 0.1 worker infrastructure** once every pre-upgrade run is
    terminal.

## Code changes you must make

- Change your `ACE_RUNTIME_FACTORY` callable to return `DjangoRuntime`
  with both registries and a `deployment_id`.
- Replace any direct `engine.handle_event(...)` /
  `engine.request_cancellation(...)` calls with `DjangoWorkflowClient`.
- Stop expecting a snapshot back from queue completion; read state via
  `DjangoWorkflowClient.get_snapshot` when needed.
- Rename settings from `SUBMISSION_ACE_*` to `ACE_*` (fallback works, but
  the legacy names are deprecated).
- Update any code matching timer status `FIRED` to `DELIVERED`.
- Handle `WorkflowStatus.BLOCKED` in any status-driven UI or reporting —
  it is nonterminal but not claimable.

## Rollback

- **Before any 1.0 runtime write:** stop services/workers and reverse
  migrations 0005→0002.
- **After any inbox/1.0 event exists:** database downgrade is
  unsupported. Disable new starts and signals, drain or deliberately block
  work, stop all 1.0 processes, restore the pre-upgrade backup, and
  redeploy 0.1 together.
- **Adapter rollback:** stop routing new runs to the adapter and
  drain/cancel only that backend's runs; never migrate a live adapter run
  into Django.
