# ace-temporal

**EXPERIMENTAL.** Temporal execution backend adapter for ACE. `ace-django`
remains the production authority.

## One authority per run

Every ACE workflow run has exactly one execution authority. Runs created by
this adapter carry `temporal-` prefixed run IDs and
`execution_backend="temporal"` metadata. Never hand a run to another backend:
the adapter raises `ForeignRunError` for any run ID it did not create
(including Django-style UUIDs and `dbos-` IDs).

## Capability matrix

| Capability | Supported |
| --- | --- |
| Core workflow transitions (start, activities, complete/fail) | Yes |
| Timers | Yes |
| Durable signals | Yes |
| Activity retry policies | Yes |
| Cancellation | Yes |
| Schedule-to-close / start-to-close timeouts | Yes |
| Heartbeat timeouts | Yes |
| Workflow deadlines | Yes |
| Django transactional activities (`TRANSACTIONAL` mode) | No |
| Queue QoS / priority / delay / partitions | No |
| Replay verification against ACE history | No |
| BLOCKED status / dead-letter queue | No |

Unsupported commands raise `UnsupportedCommand` rather than being weakly
emulated. Activities run with at-least-once semantics; do not assume external
effects happen exactly once.

## Install

```bash
pip install ace-temporal
```

Requires Python >= 3.11, `ace-core==1.*`, and `temporalio>=1.5`. The SDK is
imported lazily; the adapter's translation and validation logic (and the test
suite) work without it installed.

## Test

```bash
cd integrations/temporal
PYTHONPATH=".:../../core" python -m pytest tests -q
```
