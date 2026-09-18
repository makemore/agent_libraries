"""Pure graph transitions and integration with the real core engine/replay store."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from inspect import Parameter, signature

import pytest

from ace import (
    CancelWorkflow,
    CompleteWorkflow,
    InMemoryExecutionStore,
    InvalidTransition,
    ScheduleActivity,
    WorkflowContext,
    WorkflowEngine,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowRegistry,
    WorkflowStatus,
)
from ace.declarative import DeclarativeWorkflow, parse_graph, validate_graph
from ace.definitions import WorkflowDefinition
from ace.replay import ReplayStatus, WorkflowReplayVerifier
from tests.helpers import FROZEN_NOW, FixedClock, ThreadSafeIds

ALLOWED = frozenset({("example.process", "1")})
CONTEXT = WorkflowContext(run_id="run-1", now=FROZEN_NOW)


def wait_graph():
    return {
        "schema_version": 1,
        "entry": "wait_here",
        "nodes": {
            "wait_here": {"type": "wait", "signal": "ready", "next": "done"},
            "done": {"type": "complete", "result": {"ok": True}},
        },
    }


def activity_graph(*, deferred=False, gated=False):
    graph = {
        "schema_version": 1,
        "entry": "process",
        "nodes": {
            "process": {
                "type": "activity",
                "activity": "example.process",
                "version": "1",
                "input": {"request": "item-1", "items": [1, {"value": 2}]},
                "next": "after_work",
                "on_failure": "failed",
            },
            "after_work": {"type": "wait", "signal": "finish", "next": "done"},
            "done": {"type": "complete", "result": {"ok": True}},
            "failed": {"type": "complete", "result": {"ok": False}},
        },
    }
    if deferred:
        graph["nodes"]["process"].update({
            "completion_signal": "processed",
            "completion_match": {"request": "item-1"},
            "completion_success": {"ok": True},
        })
    if gated:
        graph["entry"] = "gate"
        graph["nodes"]["gate"] = {"type": "wait", "signal": "ready", "next": "process"}
    return graph


def event(kind, payload=None):
    return WorkflowEvent(
        event_id="event-1", run_id=CONTEXT.run_id, sequence=2,
        event_type=kind, occurred_at=FROZEN_NOW, payload=payload or {},
    )


def signal(name, body=None):
    return event(WorkflowEventType.SIGNAL_RECEIVED, {"signal_name": name, "payload": body})


def test_public_signatures_and_frozen_allowlist():
    params = signature(DeclarativeWorkflow).parameters
    assert params["name"].default == "ace.declarative"
    assert params["version"].default == "1"
    assert params["allowed_activities"].kind == Parameter.KEYWORD_ONLY
    assert params["allowed_activities"].default is Parameter.empty
    for function in (parse_graph, validate_graph):
        assert signature(function).parameters["allowed_activities"].kind == Parameter.KEYWORD_ONLY
    allowed = set(ALLOWED)
    definition: WorkflowDefinition = DeclarativeWorkflow("custom", "2", allowed_activities=allowed)
    assert (definition.name, definition.version) == ("custom", "2")
    allowed.clear()
    assert definition.start({"graph": activity_graph()}, CONTEXT).commands
    with pytest.raises(FrozenInstanceError):
        definition.name = "changed"
    with pytest.raises(TypeError):
        DeclarativeWorkflow()


@pytest.mark.parametrize("allowed", [None, ["example.process"], [("example.process",)], [("", "1")]])
def test_invalid_allowlists(allowed):
    with pytest.raises(ValueError):
        DeclarativeWorkflow(allowed_activities=allowed)


def test_empty_allowlist_and_independent_validated_copies():
    graph = wait_graph()
    validated = validate_graph(graph, allowed_activities=[])
    parsed = parse_graph(json.dumps(graph), allowed_activities=[])
    assert graph == validated == parsed
    graph["nodes"]["done"]["result"]["ok"] = False
    assert validated["nodes"]["done"]["result"] == {"ok": True}
    validated["nodes"]["done"]["result"]["ok"] = "changed"
    assert parsed["nodes"]["done"]["result"] == {"ok": True}


@pytest.mark.parametrize("value", [None, [], {}, {"schema_version": 1}, {"graph": {}}])
def test_graph_shape_is_exact(value):
    with pytest.raises(ValueError):
        validate_graph(value, allowed_activities=ALLOWED)


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2, None, []])
def test_schema_version_is_integer_one(version):
    graph = wait_graph()
    graph["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version"):
        validate_graph(graph, allowed_activities=ALLOWED)


@pytest.mark.parametrize("node_id", ["", "_start", "a.b", "a/b", " a", "a b", "é", "a" * 65])
def test_node_ids_are_bounded_slugs(node_id):
    graph = wait_graph()
    graph["nodes"][node_id] = graph["nodes"].pop("wait_here")
    graph["entry"] = node_id
    with pytest.raises(ValueError):
        validate_graph(graph, allowed_activities=ALLOWED)


@pytest.mark.parametrize("count", [0, 1, 2, 64, 65])
def test_node_count_bounds(count):
    nodes = {f"n_{i}": {"type": "wait", "signal": f"s{i}", "next": f"n_{i + 1}"}
             for i in range(max(0, count - 1))}
    if count:
        nodes[f"n_{count - 1}"] = {"type": "complete", "result": None}
    graph = {"schema_version": 1, "entry": "n_0", "nodes": nodes}
    if 2 <= count <= 64:
        assert validate_graph(graph, allowed_activities=[]) == graph
    else:
        with pytest.raises(ValueError, match="2 to 64"):
            validate_graph(graph, allowed_activities=[])


@pytest.mark.parametrize("scenario", ["missing-entry", "missing-edge", "cycle", "failure-cycle", "unreachable"])
def test_graph_connectivity_and_all_paths_terminate(scenario):
    graph = activity_graph()
    if scenario == "missing-entry":
        graph["entry"] = "missing"
    elif scenario == "missing-edge":
        graph["nodes"]["process"]["on_failure"] = "missing"
    elif scenario == "cycle":
        graph["nodes"]["after_work"]["next"] = "process"
    elif scenario == "failure-cycle":
        graph["nodes"]["process"]["on_failure"] = "process"
    else:
        graph["nodes"]["orphan"] = {"type": "complete", "result": None}
    with pytest.raises(ValueError):
        validate_graph(graph, allowed_activities=ALLOWED)


def test_joined_success_and_failure_paths_are_valid():
    graph = activity_graph()
    graph["nodes"]["failed"] = {"type": "wait", "signal": "recover", "next": "done"}
    assert validate_graph(graph, allowed_activities=ALLOWED) == graph


@pytest.mark.parametrize("kind", ["wait_here", "done"])
def test_wait_and_complete_fields_are_exact(kind):
    original = wait_graph()
    for field in original["nodes"][kind]:
        graph = deepcopy(original)
        del graph["nodes"][kind][field]
        with pytest.raises(ValueError):
            validate_graph(graph, allowed_activities=[])
    original["nodes"][kind]["expression"] = "not executable"
    with pytest.raises(ValueError):
        validate_graph(original, allowed_activities=[])


@pytest.mark.parametrize("field", ["type", "activity", "version", "input", "next", "on_failure"])
def test_activity_required_fields(field):
    graph = activity_graph()
    del graph["nodes"]["process"][field]
    with pytest.raises(ValueError):
        validate_graph(graph, allowed_activities=ALLOWED)


@pytest.mark.parametrize("field", ["queue", "retry_policy", "timeout", "priority", "callable", "import", "expression"])
def test_no_scheduling_or_executable_overrides(field):
    graph = activity_graph()
    graph["nodes"]["process"][field] = "untrusted-value"
    with pytest.raises(ValueError, match="unsupported fields"):
        validate_graph(graph, allowed_activities=ALLOWED)


@pytest.mark.parametrize("field,value", [("activity", "other"), ("version", "2"), ("input", []), ("type", "python")])
def test_activity_identity_and_input_are_validated(field, value):
    graph = activity_graph()
    graph["nodes"]["process"][field] = value
    with pytest.raises(ValueError):
        validate_graph(graph, allowed_activities=ALLOWED)


@pytest.mark.parametrize("fields", [[], ["completion_signal"], ["completion_match"], ["completion_success"],
                                  ["completion_signal", "completion_match"],
                                  ["completion_match", "completion_success"],
                                  ["completion_signal", "completion_success"]])
def test_deferred_fields_are_all_or_none(fields):
    graph = activity_graph(deferred=True)
    node = graph["nodes"]["process"]
    for field in {"completion_signal", "completion_match", "completion_success"} - set(fields):
        del node[field]
    if fields:
        with pytest.raises(ValueError, match="together"):
            validate_graph(graph, allowed_activities=ALLOWED)
    else:
        assert validate_graph(graph, allowed_activities=ALLOWED) == graph


@pytest.mark.parametrize("field,value", [
    ("completion_signal", ""), ("completion_signal", "x" * 257),
    ("completion_match", {}), ("completion_match", {"id": []}),
    ("completion_success", {"ok": {"nested": True}}),
    ("completion_success", {str(i): i for i in range(33)}),
    ("completion_success", {"ok": "x" * 4096}),
])
def test_completion_matchers_are_bounded_flat_and_correlated(field, value):
    graph = activity_graph(deferred=True)
    graph["nodes"]["process"][field] = value
    with pytest.raises(ValueError):
        validate_graph(graph, allowed_activities=ALLOWED)


@pytest.mark.parametrize("value", [[], {"": 1}, {"x" * 257: 1}, {"x": []}, {"x": {}}])
def test_wait_matcher_is_bounded_flat(value):
    graph = wait_graph()
    graph["nodes"]["wait_here"]["match"] = value
    with pytest.raises(ValueError):
        validate_graph(graph, allowed_activities=[])


class UnsafeValue:
    def __deepcopy__(self, memo):
        raise AssertionError("custom deepcopy must never run")

    def __repr__(self):
        raise AssertionError("custom repr must never run")

    def __eq__(self, other):
        raise AssertionError("custom equality must never run")


def test_container_subclasses_are_not_json():
    class CustomDict(dict):
        def __deepcopy__(self, memo):
            raise AssertionError("custom deepcopy must never run")

    class CustomList(list):
        def __iter__(self):
            raise AssertionError("custom iteration must never run")

    for value in (CustomDict(wait_graph()), CustomList()):
        with pytest.raises(ValueError, match="plain JSON"):
            validate_graph(value, allowed_activities=[])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), (1,), {1},
                                  {1: "bad-key"}, UnsafeValue(), b"bytes", "\ud800"],
                         ids=["nan", "inf", "negative-inf", "tuple", "set", "key", "custom", "bytes", "unicode"])
def test_non_json_rejected_before_copy_or_echo(value):
    graph = wait_graph()
    graph["nodes"]["done"]["result"] = value
    with pytest.raises(ValueError, match="Invalid declarative workflow"):
        validate_graph(graph, allowed_activities=[])


@pytest.mark.parametrize("container", ["list", "dict"])
def test_cyclic_python_containers_rejected(container):
    value = [] if container == "list" else {}
    if container == "list":
        value.append(value)
    else:
        value["self"] = value
    graph = wait_graph()
    graph["nodes"]["done"]["result"] = value
    with pytest.raises(ValueError, match="cyclic"):
        validate_graph(graph, allowed_activities=[])


def test_shared_noncyclic_containers_are_allowed():
    graph = wait_graph()
    shared = {"value": [1, 2]}
    graph["nodes"]["done"]["result"] = [shared, shared]
    result = validate_graph(graph, allowed_activities=[])
    shared["value"].append(3)
    assert result["nodes"]["done"]["result"] == [{"value": [1, 2]}, {"value": [1, 2]}]


@pytest.mark.parametrize("depth", [9, 10, 2000])
def test_python_json_depth_is_checked_before_recursive_copy(depth):
    graph = wait_graph()
    value = None
    for _ in range(depth):
        value = [value]
    graph["nodes"]["done"]["result"] = value
    if depth == 9:  # graph, nodes, node, then nine list containers = 12
        assert validate_graph(graph, allowed_activities=[]) == graph
    else:
        with pytest.raises(ValueError, match="depth"):
            validate_graph(graph, allowed_activities=[])


def test_exact_compact_utf8_graph_size_boundary():
    graph = wait_graph()
    graph["nodes"]["done"]["result"] = ""
    base_size = len(json.dumps(graph, separators=(",", ":")).encode())
    graph["nodes"]["done"]["result"] = "x" * (65536 - base_size)
    text = json.dumps(graph, separators=(",", ":"))
    assert len(text.encode()) == 65536
    assert parse_graph(text, allowed_activities=[]) == graph
    graph["nodes"]["done"]["result"] += "x"
    with pytest.raises(ValueError, match="size"):
        validate_graph(graph, allowed_activities=[])
    with pytest.raises(ValueError, match="size"):
        parse_graph(text + " ", allowed_activities=[])
    graph["nodes"]["done"]["result"] = "é" * 32768
    with pytest.raises(ValueError, match="size"):
        validate_graph(graph, allowed_activities=[])


@pytest.mark.parametrize("text", [
    '{"schema_version":1,"schema_version":1}',
    '{"outer":{"x":1,"x":2}}', '{"x":1,"\\u0078":2}',
    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}',
    '{"private-marker": invalid}', "[", "{", "\ud800",
])
def test_parser_rejects_duplicates_nonfinite_malformed_without_echo(text):
    with pytest.raises(ValueError) as exc:
        parse_graph(text, allowed_activities=[])
    assert "private-marker" not in str(exc.value)


def test_parser_checks_size_and_depth_before_decoder(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("decoder must not see unbounded input")
    monkeypatch.setattr("ace.declarative.json.loads", forbidden)
    for text in ["[" * 2000 + "]" * 2000, " " * 65537, "é" * 32769]:
        with pytest.raises(ValueError):
            parse_graph(text, allowed_activities=[])


def test_parser_depth_scan_understands_escaped_strings():
    graph = wait_graph()
    graph["nodes"]["done"]["result"] = '[{"quoted": "\\\\[]"}]' * 100
    assert parse_graph(json.dumps(graph), allowed_activities=[]) == graph


def test_parser_depth_boundary_matches_python_graph_validation():
    graph = wait_graph()
    value = None
    for _ in range(9):
        value = [value]
    graph["nodes"]["done"]["result"] = value
    assert parse_graph(json.dumps(graph), allowed_activities=[]) == graph
    graph["nodes"]["done"]["result"] = [value]
    with pytest.raises(ValueError, match="depth"):
        parse_graph(json.dumps(graph), allowed_activities=[])


def test_json_validation_rejects_huge_width_and_integers_before_copy():
    for value in ([None] * 65537, "x" * 65537, 1 << 300000):
        graph = wait_graph()
        graph["nodes"]["done"]["result"] = value
        with pytest.raises(ValueError, match="size"):
            validate_graph(graph, allowed_activities=[])


@pytest.mark.parametrize("input", [{}, {"graph": wait_graph(), "extra": 1}, wait_graph(), []])
def test_start_input_is_exact(input):
    with pytest.raises(ValueError, match="exactly graph"):
        DeclarativeWorkflow(allowed_activities=[]).start(input, CONTEXT)


def test_wait_only_advances_on_matching_signal():
    graph = wait_graph()
    graph["nodes"]["wait_here"]["match"] = {"request": "item-1", "ok": True, "nullable": None}
    definition = DeclarativeWorkflow(allowed_activities=[])
    start = definition.start({"graph": graph}, CONTEXT)
    assert start.commands == ()
    assert start.status == WorkflowStatus.WAITING
    assert start.state == {"graph": graph, "node": "wait_here", "completed": []}
    for incoming in [event("unknown"), event(WorkflowEventType.ACTIVITY_COMPLETED),
                     signal("other", {"request": "item-1"}), signal("ready"),
                     signal("ready", {"request": "item-1", "ok": 1, "nullable": None}),
                     signal("ready", {"request": "item-1", "ok": True})]:
        ignored = definition.advance(start.state, incoming, CONTEXT)
        assert ignored.state == start.state
        assert ignored.state is not start.state
        assert ignored.commands == ()
    completed = definition.advance(start.state, signal("ready", {
        "request": "item-1", "ok": True, "nullable": None, "extra": "not stored",
    }), CONTEXT)
    assert completed.commands == (CompleteWorkflow(result={"ok": True}),)
    assert completed.state["completed"] == ["wait_here", "done"]
    assert completed.state["graph"] == graph
    assert start.state["completed"] == []


@pytest.mark.parametrize("result", [None, True, 1, 1.25, "constant", [1, {"ok": True}]])
def test_complete_accepts_static_json_results(result):
    graph = wait_graph()
    graph["nodes"]["done"]["result"] = result
    definition = DeclarativeWorkflow(allowed_activities=[])
    start = definition.start({"graph": graph}, CONTEXT)
    complete = definition.advance(start.state, signal("ready"), CONTEXT)
    assert complete.commands == (CompleteWorkflow(result=result),)


def test_activity_schedule_inherits_every_default_and_ignores_unrelated_events():
    graph = activity_graph()
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": graph}, CONTEXT)
    assert start.commands == (ScheduleActivity(
        activity_key="process", activity_name="example.process", activity_version="1",
        input=graph["nodes"]["process"]["input"],
    ),)
    for incoming in [event("unknown"), signal("finish"),
                     event(WorkflowEventType.ACTIVITY_COMPLETED),
                     event(WorkflowEventType.ACTIVITY_COMPLETED, {"activity_key": "future"})]:
        ignored = definition.advance(start.state, incoming, CONTEXT)
        assert ignored.commands == ()
        assert ignored.state == start.state
    done = definition.advance(start.state, event(WorkflowEventType.ACTIVITY_COMPLETED, {
        "activity_key": "process", "result": {"untrusted": [1, 2]},
    }), CONTEXT)
    assert done.state == {"graph": graph, "node": "after_work", "completed": ["process"]}
    assert done.status == WorkflowStatus.WAITING
    assert done.commands == ()


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("kind,payload", [
    (WorkflowEventType.ACTIVITY_FAILED, {"failure": {"error_type": "ActivityTimeout"}}),
    (WorkflowEventType.ACTIVITY_CANCELLED, {"message": "not echoed"}),
    (WorkflowEventType.ACTIVITY_TIMED_OUT, {"final": True}),
])
def test_final_activity_failures_follow_failure_edge(deferred, kind, payload):
    graph = activity_graph(deferred=deferred)
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": graph}, CONTEXT)
    wrong = definition.advance(start.state, event(kind, {**payload, "activity_key": "other"}), CONTEXT)
    assert wrong.state == start.state and wrong.commands == ()
    failed = definition.advance(start.state, event(kind, {**payload, "activity_key": "process"}), CONTEXT)
    assert failed.state == {"graph": graph, "node": "failed", "completed": ["process", "failed"]}
    assert failed.commands == (CompleteWorkflow(result={"ok": False}),)


@pytest.mark.parametrize("final", [None, False, 1, "true"])
def test_attempt_timeout_does_not_advance_or_reschedule(final):
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": activity_graph()}, CONTEXT)
    payload = {"activity_key": "process", "timeout_type": "heartbeat"}
    if final is not None:
        payload["final"] = final
    retrying = definition.advance(start.state, event(WorkflowEventType.ACTIVITY_TIMED_OUT, payload), CONTEXT)
    assert retrying.state == start.state and retrying.commands == ()


@pytest.mark.parametrize("ack_first", [False, True])
def test_deferred_outcome_can_precede_ack_and_old_events_are_ignored(ack_first):
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": activity_graph(deferred=True)}, CONTEXT)
    ack = event(WorkflowEventType.ACTIVITY_COMPLETED, {"activity_key": "process", "result": {"ok": True}})
    outcome = signal("processed", {"request": "item-1", "ok": True})
    current = definition.advance(start.state, ack, CONTEXT) if ack_first else start
    assert current.state == start.state
    if ack_first:
        assert current.commands == ()
    for incoming in [signal("processed", {"request": "wrong", "ok": True}),
                     signal("processed", {"ok": True}), signal("other", {"request": "item-1", "ok": True})]:
        ignored = definition.advance(current.state, incoming, CONTEXT)
        assert ignored.state == start.state and ignored.commands == ()
    advanced = definition.advance(current.state, outcome, CONTEXT)
    assert advanced.state["node"] == "after_work"
    for incoming in [ack, outcome, event(WorkflowEventType.ACTIVITY_FAILED, {"activity_key": "process"})]:
        ignored = definition.advance(advanced.state, incoming, CONTEXT)
        assert ignored.state == advanced.state and ignored.commands == ()


@pytest.mark.parametrize("body", [{"request": "item-1", "ok": False}, {"request": "item-1"},
                                 {"request": "item-1", "ok": 1}])
def test_correlated_unsuccessful_outcome_takes_failure_edge(body):
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": activity_graph(deferred=True)}, CONTEXT)
    failed = definition.advance(start.state, signal("processed", body), CONTEXT)
    assert failed.commands == (CompleteWorkflow(result={"ok": False}),)


def test_empty_success_matcher_accepts_any_correlated_outcome():
    graph = activity_graph(deferred=True)
    graph["nodes"]["process"]["completion_success"] = {}
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": graph}, CONTEXT)
    advanced = definition.advance(start.state, signal("processed", {"request": "item-1"}), CONTEXT)
    assert advanced.state["node"] == "after_work"


def test_signal_matching_uses_only_plain_typed_scalars_without_traversing_body():
    graph = activity_graph(deferred=True)
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": graph}, CONTEXT)
    for incoming in [signal(UnsafeValue()), signal("processed", UnsafeValue()),
                     signal("processed", {"request": UnsafeValue()}),
                     event(WorkflowEventType.ACTIVITY_COMPLETED, {"activity_key": UnsafeValue()})]:
        ignored = definition.advance(start.state, incoming, CONTEXT)
        assert ignored.state == start.state and ignored.commands == ()
    body = {"request": "item-1", "ok": True, "unselected": UnsafeValue()}
    body["cycle"] = body
    advanced = definition.advance(start.state, signal("processed", body), CONTEXT)
    assert advanced.state["node"] == "after_work"
    assert advanced.state["graph"] == graph


def test_cancellation_has_fixed_reason_and_does_not_echo_or_traverse_payload():
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    start = definition.start({"graph": activity_graph()}, CONTEXT)
    cancelled = definition.advance(start.state, event(WorkflowEventType.CANCELLATION_REQUESTED, {
        "reason": UnsafeValue(), "body": {"do_not_store": True},
    }), CONTEXT)
    assert cancelled.commands == (CancelWorkflow(reason="Cancellation requested"),)
    assert cancelled.state == start.state
    assert cancelled.state is not start.state


def test_inputs_commands_and_every_transition_state_are_independent():
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    graph = activity_graph()
    original = deepcopy(graph)
    first = definition.start({"graph": graph}, CONTEXT)
    second = definition.start({"graph": graph}, CONTEXT)
    ignored = definition.advance(first.state, event("unknown"), CONTEXT)
    first.commands[0].input["items"].append("command mutation")
    assert first.state["graph"] == original
    graph["nodes"]["process"]["input"]["items"].append("caller mutation")
    ignored.state["graph"]["nodes"]["process"]["input"]["items"].append("state mutation")
    ignored.state["completed"].append("state mutation")
    assert first.state == second.state == {"graph": original, "node": "process", "completed": []}
    failed = definition.advance(first.state, event(WorkflowEventType.ACTIVITY_FAILED, {
        "activity_key": "process", "failure": UnsafeValue(),
    }), CONTEXT)
    failed.commands[0].result["ok"] = "result mutation"
    assert failed.state["graph"] == original
    assert first.state["completed"] == []


@pytest.mark.parametrize("field,value", [("node", "missing"), ("node", []), ("completed", ["process", "process"]),
                                       ("completed", ["missing"]), ("completed", [{}]), ("completed", "process")])
def test_invalid_state_is_rejected_without_mutating_it(field, value):
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    state = definition.start({"graph": activity_graph()}, CONTEXT).state
    state[field] = value
    original = deepcopy(state)
    with pytest.raises(ValueError):
        definition.advance(state, event("unknown"), CONTEXT)
    assert state == original


def test_state_revalidates_graph_and_completed_path():
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    state = definition.start({"graph": activity_graph()}, CONTEXT).state
    for invalid in [
        {**state, "extra": True}, {**state, "node": "after_work"},
        {**state, "completed": ["done"]}, {**state, "completed": ["process"]},
    ]:
        with pytest.raises(ValueError):
            definition.advance(invalid, event("unknown"), CONTEXT)
    state["graph"]["nodes"]["process"]["activity"] = "not-allowed"
    with pytest.raises(ValueError, match="not allowed"):
        definition.advance(state, event("unknown"), CONTEXT)


class RecordingStore(InMemoryExecutionStore):
    """Record each successful real store write for complete command replay checks."""

    def __init__(self):
        super().__init__()
        self.by_sequence = {}

    def start(self, snapshot, event, commands, *, idempotency_key):
        result = super().start(snapshot, event, commands, idempotency_key=idempotency_key)
        self.by_sequence[event.sequence] = deepcopy(commands)
        return result

    def commit(self, expected_sequence, snapshot, event, commands):
        result = super().commit(expected_sequence, snapshot, event, commands)
        self.by_sequence[event.sequence] = deepcopy(commands)
        return result


def build_declarative_engine():
    definition = DeclarativeWorkflow(allowed_activities=ALLOWED)
    registry = WorkflowRegistry()
    registry.register(definition)
    store = RecordingStore()
    return definition, WorkflowEngine(registry, store, FixedClock(), ThreadSafeIds()), store


@pytest.mark.parametrize("ack_first", [False, True])
def test_real_engine_store_ordering_and_full_replay(ack_first):
    definition, engine, store = build_declarative_engine()
    graph = activity_graph(deferred=True, gated=True)
    original = deepcopy(graph)
    started = engine.start(definition.name, {"graph": graph}, idempotency_key="declarative-1")
    assert engine.start(definition.name, {"graph": graph}, idempotency_key="declarative-1") == started
    assert started.status == WorkflowStatus.WAITING
    assert store.commands(started.run_id) == ()
    graph["nodes"]["process"]["input"]["items"].append("caller mutation")
    started.state["graph"]["nodes"]["done"]["result"]["ok"] = "snapshot mutation"
    assert store.load(started.run_id).state["graph"] == original

    def send(incoming):
        return engine.handle_event(started.run_id, incoming.event_type, payload=incoming.payload)

    outcome = signal("processed", {"request": "item-1", "ok": True, "extra": "untrusted-body"})
    assert send(outcome).state["node"] == "gate"  # No future-event buffering.
    running = send(signal("ready"))
    assert running.status == WorkflowStatus.RUNNING
    assert running.state["node"] == "process"
    assert send(signal("ready")).state == running.state  # Duplicate old wait signal.
    ack = event(WorkflowEventType.ACTIVITY_COMPLETED, {
        "activity_key": "process", "result": {"untrusted-output": True},
    })
    if ack_first:
        assert send(ack).state["node"] == "process"
    after_work = send(outcome)
    assert after_work.state["completed"] == ["gate", "process"]
    if not ack_first:
        assert send(ack).state == after_work.state
    assert send(outcome).state == after_work.state
    assert send(event(WorkflowEventType.ACTIVITY_FAILED, {"activity_key": "process"})).state == after_work.state
    complete = send(signal("finish"))
    assert complete.status == WorkflowStatus.COMPLETED
    assert complete.result == {"ok": True}
    assert complete.state == {"graph": original, "node": "done", "completed": ["gate", "process", "after_work", "done"]}
    assert store.load(started.run_id) == complete
    commands = store.commands(started.run_id)
    assert len(commands) == 2
    assert isinstance(commands[0], ScheduleActivity)
    assert commands[0].input == original["nodes"]["process"]["input"]
    assert commands[1] == CompleteWorkflow(result={"ok": True})
    replay = WorkflowReplayVerifier().replay(
        definition, store.load(started.run_id), store.history(started.run_id), store.by_sequence,
    )
    assert replay.status == ReplayStatus.PASSED
    assert all(item.commands_available and item.status == ReplayStatus.PASSED for item in replay.events)
    with pytest.raises(InvalidTransition, match="terminal"):
        send(outcome)


@pytest.mark.parametrize("cancel", [False, True])
def test_real_engine_failure_or_cancellation_and_replay(cancel):
    definition, engine, store = build_declarative_engine()
    started = engine.start(definition.name, {"graph": activity_graph()})
    if cancel:
        final = engine.request_cancellation(started.run_id, reason="not echoed")
        assert final.status == WorkflowStatus.CANCELLED
        assert final.result == {"reason": "Cancellation requested"}
        assert final.state == started.state
    else:
        retrying = engine.handle_event(started.run_id, WorkflowEventType.ACTIVITY_TIMED_OUT, payload={
            "activity_key": "process", "timeout_type": "heartbeat",
        })
        assert retrying.state == started.state
        final = engine.handle_event(started.run_id, WorkflowEventType.ACTIVITY_FAILED, payload={
            "activity_key": "process", "failure": {"error_type": "ActivityTimeout", "message": "not echoed"},
        })
        assert final.status == WorkflowStatus.COMPLETED
        assert final.result == {"ok": False}
    assert len(store.commands(started.run_id)) == 2
    report = WorkflowReplayVerifier().replay(definition, final, store.history(final.run_id), store.by_sequence)
    assert report.status == ReplayStatus.PASSED