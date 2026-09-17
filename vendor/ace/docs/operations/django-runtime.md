# Operating the Django runtime

Every management command below lives in `ace_django` and runs via
`python manage.py <command>`.

## runaceworker — activity workers

Supervisor that spawns N single-slot worker processes.

```bash
python manage.py runaceworker --workers 4 --queues medium,high \
    --poll-interval 1.0 --lease-seconds 300 --renewal-seconds 60 \
    --shutdown-timeout 30 --worker-id-prefix ace-prod
```

| Flag | Default | Meaning |
|---|---|---|
| `--workers` | 4 | Number of worker processes |
| `--queues` | `medium` | Comma-separated queue names to claim from |
| `--poll-interval` | 1.0 | Seconds between empty-claim polls |
| `--lease-seconds` | 300 | Claim lease duration |
| `--renewal-seconds` | 60 | Background lease-renewal interval |
| `--shutdown-timeout` | 30 | Grace period on drain before hard stop |
| `--worker-id-prefix` | auto | Stable prefix for worker IDs / heartbeats |

Each worker claims one activity at a time, renews its lease in a
background thread, and records an `AceWorkerHeartbeat` (role `WORKER`)
with its advertised activity/workflow capabilities, backend, and
deployment ID every iteration. SIGINT/SIGTERM drain gracefully; a worker
process that exits unexpectedly fails the command.

## runaceservices — dispatcher, timers, deadlines, timeouts

```bash
python manage.py runaceservices --roles dispatcher,timer,deadline,timeouts \
    --poll-interval 1.0 --worker-id-prefix ace-svc
```

| Flag | Default | Meaning |
|---|---|---|
| `--roles` | all four | Comma-separated subset of `dispatcher,timer,deadline,timeouts` |
| `--poll-interval` | 1.0 | Seconds between idle polls per role |
| `--worker-id-prefix` | auto | Prefix for role heartbeat IDs |
| `--max-iterations` | none | Stop a role after N idle polls (testing only) |

Each role runs in its own thread and records a role-tagged heartbeat
(`DISPATCHER`, `TIMER`, `DEADLINE`; the `timeouts` role heartbeats as
`DEADLINE`) every iteration, so `checkacehealth --readiness` can verify
liveness. The `dispatcher` role requires `ACE_RUNTIME_FACTORY` to return a
`DjangoRuntime` with a workflow registry; the other roles need no
registry. A transient DB error in heartbeat recording never kills the
loop. SIGINT/SIGTERM drain gracefully.

Run at least one `runaceservices` process alongside your workers —
without a dispatcher, inbox events accumulate and runs never advance.

## checkacehealth — readiness gates and alerting

```bash
# Deploy gate: can the system accept new work right now?
python manage.py checkacehealth --readiness --fail-on-unready

# Alerting: historical failure counts (never fails the command)
python manage.py checkacehealth --failure-metrics

# Legacy full report (queues, leases, failures, heartbeats)
python manage.py checkacehealth --fail-on-unhealthy

# Single-queue compact JSON for cloud log ingestion
python manage.py checkacehealth --queue medium --compact --fail-on-routing-unready
```

- `--readiness` (`collect_ace_readiness`) checks database access, fresh
  dispatcher/timer/deadline heartbeats, queue configs, and worker
  coverage. Historical failures are deliberately ignored — a past DLQ must
  not block a deploy. Pair with `--fail-on-unready` for exit-code gating.
- `--failure-metrics` (`collect_ace_failure_metrics`) counts BLOCKED runs,
  inbox/timer dead letters, transition failures, timeouts, and failed
  runs/activities/groups. Feed it to dashboards and alerts.
- The default report (`collect_ace_health`) combines both concerns and
  honors `ACE_EXPECTED_QUEUES`, `ACE_HEARTBEAT_STALE_SECONDS`,
  `ACE_OLDEST_READY_SECONDS`, and `ACE_WORKERS_EXPECTED` (legacy
  `SUBMISSION_ACE_*` names are read as fallback).

## checkacereconciliation — impossible-state detection

```bash
python manage.py checkacereconciliation --fail-on-inconsistency \
    --terminal-window-seconds 3600
```

Reports (never mutates) states that should be impossible: claimable
members under terminal groups, running groups inside terminal workflows,
counters disagreeing with member rows. Terminal scans are windowed so the
check stays fast as history grows.

## syncacequeues — queue config provisioning

```bash
python manage.py syncacequeues --dry-run
python manage.py syncacequeues --disable-unlisted
```

Creates/updates `QueueConfig` rows from the `ACE_QUEUES` setting (dict of
queue name → `enabled`, `global_concurrency`, `rate_limit_count`,
`rate_limit_period_seconds`, `partition_concurrency`). Never deletes;
`--disable-unlisted` disables enabled queues absent from settings. With no
`ACE_QUEUES` setting, a default enabled `medium` queue is provisioned.
Activities scheduled to a missing or disabled queue fail loudly at
materialization — run this before starting services.

## replayaceworkflow — determinism verification

```bash
python manage.py replayaceworkflow <run_id> --registry myproject.ace_runtime.workflow_registry
python manage.py replayaceworkflow <run_id> --json -v
```

Re-executes the workflow definition against persisted history and reports
`PASSED`, `PARTIAL` (state verified but some events lack persisted
commands — legacy 0.1 rows), `FAILED` (drift detected), or `ERROR`.
`--registry` is a dotted path to the workflow registry; `--json` and
`-v`/`--verbose` control output. Exit code 1 on FAILED/ERROR.

## retryaceinbox / resumeaceworkflow — recovery

```bash
# Inspect and reset dead-lettered inbox events on non-blocked runs
python manage.py retryaceinbox <run_id> --dry-run
python manage.py retryaceinbox --all --max-count 100

# Unblock a BLOCKED run: restore status, reset DLQ events, clear metadata
python manage.py resumeaceworkflow <run_id> --dry-run
python manage.py resumeaceworkflow <run_id> --force   # skip confirmation
```

`retryaceinbox` resets `DEAD_LETTER` events to `RETRYING` with attempts
zeroed; it excludes terminal and BLOCKED runs (those need
`resumeaceworkflow` first). `resumeaceworkflow` validates the run is
BLOCKED, restores `blocked_from_status`, clears blocking metadata, and
resets all dead-lettered events in one transaction; without `--force` it
prompts interactively.

## purgeacehistory — data retention

```bash
python manage.py purgeacehistory --min-age-days 90                # dry run
python manage.py purgeacehistory --min-age-days 90 --confirm \
    --batch-size 100 --max-batches 10 --namespace billing
```

Safety rules, enforced in the command:

- Dry-run by default — deletes nothing without `--confirm`.
- `--min-age-days` is required (minimum 1); only runs completed before the
  cutoff qualify.
- Only terminal runs (`COMPLETED`, `FAILED`, `CANCELLED`) are eligible;
  `BLOCKED` runs are always excluded.
- Runs with pending/retrying/dead-lettered inbox events or
  scheduled/retrying/dead-lettered timers are excluded.
- Whole runs are deleted (cascading events/timers/inbox), never individual
  events — partial history would break replay.
- Deletion happens in batches (`--batch-size`, `--max-batches`) to avoid
  long transactions; `--namespace` scopes the purge.
