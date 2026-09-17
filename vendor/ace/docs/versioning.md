# Workflow and activity versioning

ACE routes work by `(name, version)` identity. This document explains the
rules that make deploys safe while runs are in flight.

## The core rule

**Never change the behavior of a published `(name, version)`.** A workflow
run pins `workflow_version` at start and replays against that exact
definition; editing it in place breaks determinism for every in-flight and
historical run. To change behavior, publish a new version and register
both side by side until old runs drain.

## How routing works

Registries (`WorkflowRegistry`, `ActivityRegistry`) key definitions by
`(name, version)` and expose `identities()` / `has()` for routing:

- **Workers** advertise their registered activity `(name, version)` pairs
  in heartbeats and pass them to `claim()` — a worker never claims an
  activity version it cannot execute. Rows for unadvertised versions stay
  `READY` until a capable worker polls.
- **The dispatcher** only selects inbox events for runs whose
  `(workflow_name, workflow_version)` is registered in its runtime.
  Events for unregistered versions stay `PENDING` — no recovery budget is
  spent, nothing is dead-lettered, and other runs are not starved. They
  dispatch normally once a service with that version starts.

`DjangoRuntime` is where capabilities come from:

```python
DjangoRuntime(
    activities=activity_registry,   # advertised by workers
    workflows=workflow_registry,    # advertised by dispatchers
    deployment_id="deploy-2026-08-18",
)
```

## Deploying a new workflow version

1. Register the new version **alongside** the old one in the runtime
   factory (both `("my_workflow", "1")` and `("my_workflow", "2")`).
2. Deploy services and workers.
3. Point new starts at version 2.
4. Monitor version coverage: `checkacehealth --readiness` reports active
   runs whose versions no capable service advertises.
5. Remove version 1 from the registry only after every version-1 run is
   terminal.

Removing a version too early is safe but visible: affected runs simply
stall with `PENDING` inbox events (or `READY` activities) until the
version is re-registered.

## Changing activity behavior

The same rule applies to activities: publish `("my_activity", "2")` and
schedule it from a new workflow version. Old workflow versions keep
scheduling `("my_activity", "1")`, so keep it registered until those runs
drain.

## What counts as a behavior change

Anything that alters the commands or state a definition produces for the
same inputs and events: changed activity names/inputs, reordered
scheduling, different retry policies, new timers, changed state shape.
Pure refactors that provably emit identical transitions are technically
safe, but `replayaceworkflow` on a sample of historical runs is the only
trustworthy check — if replay reports `FAILED`, the change was not pure.

## Version identity in persistence

- `WorkflowRun.workflow_version` — pinned at start, never rewritten.
- `ActivityRun.activity_name` / `activity_version` — pinned at
  materialization from `ScheduleActivity`.
- `AceWorkerHeartbeat.workflow_capabilities` / `activity_capabilities` —
  what each live process advertises, for observability and readiness.
