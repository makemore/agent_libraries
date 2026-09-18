# Durability architecture

How ACE guarantees that no accepted event is ever lost, applied twice, or
applied out of order — across worker crashes, dispatcher crashes, and
database rollbacks.

## Inbox/outbox design

Every input to a workflow run becomes a durable `WorkflowInboxEvent` row
before it is applied:

- **Activity and group outcomes** — `DjangoActivityQueue.complete/fail/cancel`
  and group fan-in commit the source outcome (attempt row, activity status)
  *and* exactly one pending inbox event in the same database transaction
  (`ace_django/inbox.py:enqueue_locked`). This is the outbox pattern with
  no separate outbox table: source and inbox share one commit.
- **Timers** — `DjangoTimerService` marks a due timer `DELIVERED` and
  inserts its inbox event atomically.
- **Signals** — `DjangoWorkflowClient.signal` locks the run, allocates a
  sequence, and inserts durably. It never calls the engine.
- **Deadlines and cancellation** — `DjangoDeadlineService` and
  `request_cancellation` enter the same path.

Two unique constraints make delivery idempotent:

- `(workflow_run, inbox_sequence)` — the per-run order is total and gap-free
  (`WorkflowRun.last_inbox_sequence` is allocated under the run lock).
- `(workflow_run, source_type, source_key)` — re-inserting the same source
  returns the existing row if payload/actor/event_type match, and raises
  `DuplicateInboxEvent` if they differ.

## Dispatcher commit protocol

`DjangoWorkflowDispatcher.dispatch_once` processes one event at a time:

1. Non-locking candidate query finds each run's earliest unresolved sequence
   (`PENDING`, `RETRYING`, or `DEAD_LETTER`), then checks availability, version
   support, and run status (terminal and `BLOCKED` runs excluded). A delayed
   or dead-lettered head prevents overtaking within its run without hiding
   ready events in other runs; dead letters require operator recovery.
2. Outer transaction locks the `WorkflowRun`, then the `WorkflowInboxEvent`,
   and re-validates: the event must still be pending/retrying, available, and
   the run's earliest unresolved sequence (**no overtaking**); run status and
   version support are checked again. Stale candidates cannot bypass retry
   backoff, reapply an acknowledged event, or overwrite its acknowledgement.
3. An inner **savepoint** wraps `WorkflowEngine.apply_event(snapshot, event)`.
   The core event is built from the inbox row: its stable UUID becomes the
   event ID and `run.last_event_sequence + 1` its sequence.
4. On success, one commit persists: the updated snapshot, the immutable
   `WorkflowEvent` row (including canonically serialized commands for
   replay), materialized command rows (activities, groups, timers), and
   the inbox row marked `PROCESSED`.
5. On exception, the savepoint rolls back everything from step 3, and the
   outer transaction records a `WorkflowTransitionFailure` plus the retry
   or dead-letter state.

Because failed savepoints roll back completely and `(run, inbox_sequence)`
plus the stable event ID guard commits, dispatcher retries can never
duplicate history or commands.

## Recovery budget → BLOCKED / DLQ

Transition failures are retried with exponential backoff (default: 5
attempts, 1s initial delay, 2x multiplier, 300s cap). When the budget is
exhausted:

- the inbox event is marked `DEAD_LETTER`,
- the `WorkflowTransitionFailure` resolution is recorded as `blocked`,
- the run's status moves to `BLOCKED`, remembering `blocked_from_status`,
  `blocked_at`, and a bounded `block_reason`.

`BLOCKED` is nonterminal but not claimable: the dispatcher and timer
service skip the run, and new signals are rejected. Later outcomes remain
queued in the inbox. Operators recover with `resumeaceworkflow` (restores
the prior status and resets dead-lettered events) or inspect with
`retryaceinbox --dry-run`. Timer *delivery* failures have their own
retry budget and timer-level `DEAD_LETTER` state.

## Crash-boundary reasoning

For every crash point, either the whole step committed or none of it did:

| Crash between … | Outcome |
|---|---|
| activity work and completion commit | lease expires; another worker re-executes (at-least-once invocation) |
| completion commit and dispatch | inbox event is durable and `PENDING`; the next dispatcher picks it up |
| savepoint success and outer commit | nothing visible; the event is still pending and is re-dispatched with the same event ID |
| timer `DELIVERED` mark and inbox insert | impossible — same transaction |
| signal insert and client response | caller retries with the same idempotency key and receives the same receipt |

A database outage may prevent failure *telemetry* from being written, but
it can never partially commit a transition.

## Activity claim lock scope

`DjangoActivityQueue.claim` selects one candidate at a time with a limited
`SELECT FOR UPDATE SKIP LOCKED` query on PostgreSQL. Iterating an unrestricted
Django queryset would eagerly lock the whole ready set before returning its
first row, preventing concurrent workers from claiming unrelated activities.

Queue configuration locks, priority/availability ordering, capability filters
and concurrency/rate/partition checks are unchanged. If a candidate fails a
partition check, the next query excludes that row and continues. Rejected row
locks remain held until the short claim transaction ends; this can require
additional queries for heavily backed-up partitions.

`test_claim_lock_scope.py` holds a real claim transaction open in another process
and requires a second worker to claim another ready row without polling. The
existing four-process claim, partition-limit and version-routing tests remain
part of the PostgreSQL validation suite.

## Lock ordering

All multi-row transactions acquire locks strictly top-down:

```
QueueConfig → WorkflowRun → ActivityGroupRun → ActivityRun
    → ActivityAttempt → WorkflowTimer → WorkflowInboxEvent
```

Multiple rows of one type are locked in name/UUID order. Append-only rows
(`WorkflowEvent`, `WorkflowTransitionFailure`) are inserted after locks
and never explicitly locked. Workflow/timer candidate queries are non-locking
hints: each service re-reads and validates rows after acquiring locks in order.
Activity claim selection acquires its limited candidate lock as described above.
Application rows touched by transactional activities are locked only
after all ACE rows — callers must never lock app rows and then enter ACE.
