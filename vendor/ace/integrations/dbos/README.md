# ace-dbos

**EXPERIMENTAL** DBOS execution backend for ACE. `ace-django` remains the
production authority.

## One authority per run

Every workflow run is owned by exactly one backend. Runs created by this
adapter use `dbos-` prefixed IDs and are stamped with
`execution_backend="dbos"`. Never share a run with the Django or Temporal
backends — the adapter raises `ForeignRunError` for any run ID it did not
create.

## Capability matrix

| Capability | DBOS |
| --- | --- |
| Core workflow transitions | ✅ |
| Timers | ✅ (durable sleep) |
| Durable signals | ✅ |
| Retry policies | ✅ |
| Cancellation | ✅ |
| Django transactional activities | ❌ |
| Queue QoS / priority / delay / partitions | ❌ |
| Heartbeat timeouts | ❌ |
| Replay verification | ❌ |
| BLOCKED / dead-letter queue | ❌ |

Unsupported commands are rejected with `UnsupportedCommand` rather than
weakly emulated. Activity execution is at-least-once; do not assume
exactly-once semantics for external effects.

## Install

```bash
pip install ace-dbos
```

## Test

```bash
cd integrations/dbos
PYTHONPATH=".:../../core" python -m pytest tests -q
```

Tests that require the DBOS SDK are skipped when `dbos` is not installed.
