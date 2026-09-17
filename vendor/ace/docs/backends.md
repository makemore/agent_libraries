# Execution backends

ACE definitions are pure (`ace-core`); execution backends make them
durable. **`ace-django` is the production authority.** The DBOS and
Temporal adapters are **experimental**: they execute `ace-core`
definitions on their own durability substrates, are package-isolated, and
are suitable only for newly created, explicitly selected runs.

## One authority per run

Every workflow run is owned by exactly one backend, stamped at creation:

- Django runs: UUID run IDs, `WorkflowRun.execution_backend="django"`.
- DBOS runs: `dbos-` prefixed IDs, `execution_backend="dbos"`.
- Temporal runs: `temporal-` prefixed IDs, `execution_backend="temporal"`.

Adapters raise `ForeignRunError` for any run ID they did not create, and
they never import or call `DjangoExecutionStore` / `DjangoWorkflowClient`.
Correlation IDs may match across systems; run IDs and authority may not.
Cross-backend run migration is unsupported — to roll back an adapter,
stop routing new runs to it and drain or cancel its existing runs.

## Capability matrix

Declared in `ace/backend.py` (`DJANGO_CAPABILITIES`, `DBOS_CAPABILITIES`,
`TEMPORAL_CAPABILITIES`). Adapters validate every emitted command against
their capabilities and reject unsupported operations with
`UnsupportedCommand` rather than silently degrading.

| Capability | Django | DBOS | Temporal |
|---|---|---|---|
| Core workflow transitions | ✅ | ✅ | ✅ |
| Durable inbox / ordered dispatch | ✅ | ❌ (native durability) | ❌ (native durability) |
| Queue QoS (concurrency, rate limits, partitions) | ✅ | ❌ | ❌ |
| Activity priority / delay | ✅ | ❌ | ❌ |
| Transactional activities (`TRANSACTIONAL` mode) | ✅ | ❌ | ❌ |
| Schedule-to-close / start-to-close timeouts | ✅ | ✅ | ✅ |
| Heartbeat timeouts | ✅ | ❌ | ✅ |
| Workflow deadlines | ✅ | ❌ | ✅ |
| Durable signals | ✅ | ✅ | ✅ |
| Timers | ✅ | ✅ | ✅ |
| Replay verification against ACE history | ✅ | ❌ | ❌ |
| BLOCKED status / recovery commands | ✅ | ❌ | ❌ |
| Dead-letter queue | ✅ | ❌ | ❌ |

No backend makes arbitrary external side effects exactly once. Activity
invocation is at-least-once everywhere; see
[delivery-guarantees.md](delivery-guarantees.md).

## Packaging and dependencies

| Package | Path | Depends on |
|---|---|---|
| `ace-core` | `core/` | stdlib only |
| `ace-django` | `django/` | `ace-core`, Django 5.2, PostgreSQL for production |
| `ace-dbos` | `integrations/dbos/` | `ace-core==1.*`, `dbos` |
| `ace-temporal` | `integrations/temporal/` | `ace-core==1.*`, `temporalio>=1.5` |

Neither adapter depends on Django, and `ace-django` never imports the
adapters. Adapter SDKs are imported lazily; each adapter's translation and
validation logic (and test suite) works without its SDK installed.

## The shared contract

`ace/testing/backend_contract.py` defines `BackendContractTest` — the
suite every adapter must pass: start and event ordering, activity
success/failure, timers, idempotent signals, retries, cancellation,
unsupported-capability rejection, serialization round-trips, and authority
isolation. Adapter tests run independently:

```bash
cd integrations/dbos     && PYTHONPATH=".:../../core" python -m pytest tests -q
cd integrations/temporal && PYTHONPATH=".:../../core" python -m pytest tests -q
```

## Choosing a backend

Use Django. It is the only backend with the full durability feature set,
operational tooling (`checkacehealth`, `replayaceworkflow`, recovery
commands), and production test coverage. Use an adapter only when you
already operate that substrate, only for new opt-in runs, and only for
workflows that need none of the unsupported capabilities above.
