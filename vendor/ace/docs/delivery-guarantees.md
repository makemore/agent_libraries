# Delivery guarantees

The exact contract. Anything not listed here is not guaranteed.

## The contract

- Standard activity invocation is **at least once**. A worker that
  crashes, loses its lease, or times out will have its activity reclaimed
  and re-executed by another worker.
- Attempt completion and inbox acceptance are **exactly once in ACE
  persistence**, enforced by lease fencing and source uniqueness.
- Signals are **idempotently accepted**.
- Transactional mode atomically couples same-database writes with activity
  completion — but **invocation is still at least once**.
- **No guarantee makes arbitrary external side effects exactly once.**
  If an activity sends an email or calls an external API, it may do so
  more than once. Make external effects idempotent yourself.

## What lease fencing does — and does not — guarantee

Every claim (`DjangoActivityQueue.claim`) issues a fresh
`ownership_token` on the `ActivityAttempt` and a lease deadline. On
`complete`/`fail`, the token is checked under lock:

- A worker whose lease expired and whose activity was reclaimed gets
  `LeaseOwnershipLost` — its result is rejected. Only one attempt's
  outcome is ever recorded, so **completion recording is exactly-once**.
- Fencing does **not** stop the stale worker's Python code from running to
  completion — synchronous code cannot be safely preempted. Both the stale
  and the fresh worker may fully execute the activity body; only one gets
  to record the outcome. External side effects from the loser still
  happened.

The same applies to cancellation and timeouts: they are cooperative
(`context.check_cancelled()`, heartbeat deadlines). Leases fence late
results; code may continue until it returns or the process exits.

## Inbox acceptance

Each workflow input is inserted exactly once per
`(workflow_run, source_type, source_key)`:

- activity/group outcomes use the activity/group key,
- timers use the timer key,
- signals use the caller's idempotency key,
- cancellation uses a fixed key (one cancellation request per run).

A duplicate insertion with an identical event type, payload, and actor
returns the existing row; the dispatcher then applies each accepted event
exactly once to workflow history (see
[architecture/durability.md](architecture/durability.md)).

## Signal idempotency

`DjangoWorkflowClient.signal(run_id, signal_name, payload, *,
idempotency_key=...)` requires a caller-supplied idempotency key:

- Retrying with the same key, name, payload, and actor returns the same
  receipt (`SignalReceipt.idempotent_retry=True`) and creates no second
  event. Safe for response-loss retries.
- Retrying with the same key but a **different** signal name, payload, or
  actor raises `DuplicateInboxEvent` — the conflict is surfaced, never
  silently merged.

Signals to terminal runs are rejected; signals to `BLOCKED` runs are
rejected until the run is resumed.

## Transactional activity mode — scope

`ScheduleActivity(execution_mode=ActivityExecutionMode.TRANSACTIONAL)`
runs the callable inside one database transaction
(`DjangoTransactionalActivityExecutor`): the callable's same-database ORM
writes, the activity/attempt outcome, and the inbox event commit together,
or all roll back together on exception.

Scope limits:

- Only writes routed to the ACE database are covered. Writes to another
  database alias or to external systems are outside the guarantee.
- The callable must be short and synchronous — no heartbeat thread, no
  network expectations.
- Invocation remains at-least-once: after a rollback, the callable may be
  invoked again on retry.

Standard (`STANDARD`) activities keep at-least-once semantics with
external side effects allowed.
