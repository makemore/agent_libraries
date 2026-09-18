# Declarative JSON workflows

`ace.declarative.DeclarativeWorkflow` implements ACE's `WorkflowDefinition`
protocol. It interprets a small, validated JSON graph, not Python code. It has
no I/O, imports selected by graph data, expressions, templates, or callable nodes.
Activities are ordinary host-registered ACE activities, selected from an explicit
allowlist of exact `(name, version)` pairs.

## Public API

<augment_code_snippet mode="EXCERPT">
````python
from ace.declarative import DeclarativeWorkflow, parse_graph, validate_graph

allowed = {("example.process", "1")}
definition = DeclarativeWorkflow(allowed_activities=allowed)
graph = parse_graph(json_text, allowed_activities=allowed)
registry.register(definition)
snapshot = engine.start(definition.name, {"graph": graph})
````
</augment_code_snippet>

The constructor is `DeclarativeWorkflow(name='ace.declarative', version='1', *,
allowed_activities=...)`. The allowlist is required, copied into a frozen set,
and cannot be expanded by the graph. An empty allowlist permits wait/complete
graphs. Register the selected activity implementations separately with the host.

`validate_graph(graph, *, allowed_activities)` accepts a Python JSON value and
returns an independent validated copy. `parse_graph(text, *, allowed_activities)`
accepts JSON text and also rejects duplicate keys at every level. Both raise
`ValueError` with messages that do not echo submitted data. Start input must
contain **exactly** `{"graph": graph}`; `start` validates again.

At an untrusted boundary, parse/validate **before** passing input to the engine
or persistence adapter: those layers may copy or persist input before calling a
definition. Validation is not a secret scrubber; never submit credentials in a
graph or activity input.

## Schema version 1

The graph contains exactly `schema_version` (integer `1`), `entry` (node ID), and
`nodes` (an object keyed by node ID). There must be **2–64 nodes**, all reachable
from entry, with no cycles, including failure edges. Every edge must resolve and
every structural path must terminate at a complete node. External events can
still leave a run waiting indefinitely.

Limits are checked before recursive copying or JSON decoding:

- Maximum graph size: **65,536 bytes**, measured as compact UTF-8 JSON with
  non-ASCII characters unescaped. JSON text also has a 65,536-byte limit including
  whitespace; escape-heavy text may therefore be rejected even when its decoded
  graph would fit.
- Maximum JSON container nesting: **12**, counting the graph object as level 1.
- Node IDs: **1–64** ASCII letters/digits, `_`, or `-`, starting with a letter/digit.
- Signal names, activity names/versions, and matcher keys: **1–256** characters,
  nonblank. Names are literal labels, never import paths to execute.
- Matchers: flat JSON objects, at most **32** scalar entries and **4,096 bytes**.
- Only plain JSON objects, arrays, strings, finite numbers, booleans, and null
  are accepted. Cyclic Python containers, non-string keys, subclasses/custom
  objects, tuples, NaN, infinities, and invalid Unicode are rejected. Integer
  decoding/encoding also observes Python's integer-string safety limit.

Unknown or missing fields are errors. Queue, retry, timeout, priority, and other
scheduling overrides are not part of this schema.

### Wait

<augment_code_snippet mode="EXCERPT">
````json
{"type": "wait", "signal": "ready", "match": {"request": "item-1"}, "next": "process"}
````
</augment_code_snippet>

Required fields: `type`, `signal`, `next`. Optional: `match` (empty by default).
Entering a wait node emits no commands and sets `WAITING`. Only
`SIGNAL_RECEIVED` with the exact signal name and matching body advances it.

Signals use ACE's envelope `{"signal_name": "ready", "payload": {...}}`.
Matcher keys are compared against the inner `payload`, not the envelope. All
configured keys must exist with equal scalar values **and types** (`true` is not
`1`, and integer `1` is distinct from float `1.0`). Extra body keys are ignored.
An empty matcher imposes no body restriction, including on null/scalar bodies.

### Activity

<augment_code_snippet mode="EXCERPT">
````json
{"type": "activity", "activity": "example.process", "version": "1",
 "input": {"request": "item-1"}, "next": "done", "on_failure": "failed"}
````
</augment_code_snippet>

All shown fields are required; input must be a JSON object. Entering the node
emits one `ScheduleActivity`, with `activity_key` equal to the node ID. Only its
name, version, and copied input are supplied in addition to the key; **all other
command defaults are inherited**, including retries and queue selection.
Activities retain ACE's at-least-once execution contract, not exactly-once side
effects. Hosts must make side effects idempotent as appropriate.

Normally, `ACTIVITY_COMPLETED` with a matching `activity_key` takes `next`.
Matching `ACTIVITY_FAILED` or `ACTIVITY_CANCELLED` takes `on_failure`. ACE Django's
timeout service retries attempt timeouts internally and emits `ACTIVITY_FAILED`
when exhausted. An adapter emitting `ACTIVITY_TIMED_OUT` must include the boolean
`final: true` to take `on_failure`; unmarked or nonfinal timeouts are ignored.

For activities that dispatch external jobs, add **all three** completion fields:

<augment_code_snippet mode="EXCERPT">
````json
{"type": "activity", "activity": "example.process", "version": "1",
 "input": {"request": "item-1"}, "next": "done", "on_failure": "failed",
 "completion_signal": "processed", "completion_match": {"request": "item-1"},
 "completion_success": {"ok": true}}
````
</augment_code_snippet>

`completion_match` **must be nonempty** for correlation. Dispatch completion
(the activity ACK) is ignored. A matching completion signal takes `next` if
`completion_success` matches, otherwise `on_failure`. Missing success keys count
as failure; an empty success matcher always succeeds. The external outcome can
arrive **before the ACK**. Final dispatch failures still take `on_failure` if
the node is current. Once advanced, old ACKs/failures do not change its outcome.

### Complete

<augment_code_snippet mode="EXCERPT">
````json
{"type": "complete", "result": {"ok": true}}
````
</augment_code_snippet>

Exactly `type` and `result` are required; result can be any bounded JSON value.
Entering emits `CompleteWorkflow` with a separate copy of that static result.
No activity outputs or event bodies are substituted into it.

## State, replay, and trust

State contains exactly `graph` (validated independent copy), `node` (current
node ID), and `completed` (finished IDs in traversal order, including the terminal
complete node when reached). Each transition returns independent JSON state;
neither input, earlier state, nor command payloads alias the new graph. The
definition itself is frozen; returned JSON remains ordinary mutable JSON.

Unknown events and events for old/future nodes or nonmatching signals emit no
commands and preserve state content. Future signals are not buffered. Hosts
should use distinct correlation values per node and durable signal idempotency
keys: two nodes deliberately configured with the same signal/matcher cannot
distinguish an old delivery from a new one. The engine rejects events after a
terminal outcome. A cancellation request emits
`CancelWorkflow(reason='Cancellation requested')` without echoing its payload.

The host is responsible for authenticating signals, authorizing graph submission
and activity side effects, and validating domain-specific activity input.
**Success matchers and correlation are routing, not authorization.** The runner
does not copy event bodies, activity results, or failure details into state, but
ACE's normal event history may retain them; existing retention/privacy policies
still apply. Nothing here changes persistence, sharing, or authorization defaults.

Keep the definition version, allowlist, and referenced activity versions available
for persisted runs. Validate changes with `WorkflowReplayVerifier` against stored
events and per-event commands. The core tests cover pure transitions and real
`WorkflowEngine`/`InMemoryExecutionStore` replay, including both ACK/outcome orders.