"""Bounded JSON workflow graphs with pure, explicitly allowlisted transitions."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import cast

from ace.commands import CancelWorkflow, CompleteWorkflow, ScheduleActivity, WorkflowTransition
from ace.json_types import JsonObject, JsonValue
from ace.models import WorkflowContext, WorkflowEvent, WorkflowEventType, WorkflowStatus

__all__ = ["DeclarativeWorkflow", "parse_graph", "validate_graph"]

_MAX_BYTES = 64 * 1024
_MAX_DEPTH = 12
_MAX_NODES = 64
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_DEFERRED = {"completion_signal", "completion_match", "completion_success"}


def _invalid(message: str) -> ValueError:
    # Never interpolate submitted values: adapters may persist exception messages.
    return ValueError("Invalid declarative workflow: " + message)


def _scalar_size(value: object, limit: int) -> int:
    kind = type(value)
    if kind not in (str, int, float, bool, type(None)):
        raise _invalid("only plain JSON values are supported")
    if kind is float and not math.isfinite(cast(float, value)):
        raise _invalid("numbers must be finite")
    if kind is str and len(cast(str, value)) > limit:
        raise _invalid("JSON size limit exceeded")
    if kind is int and cast(int, value).bit_length() > limit * 4:
        raise _invalid("JSON size limit exceeded")
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, UnicodeError):
        raise _invalid("invalid JSON scalar") from None


def _check_json(value: object, *, max_bytes: int = _MAX_BYTES) -> None:
    """Check before deepcopy/encoding; traversal space is bounded by depth, not width."""
    size = 0
    active: set[int] = set()
    stack: list[tuple[Iterator[object], int, int | None]] = [(iter((value,)), 0, None)]
    while stack:
        iterator, depth, identity = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            if identity is not None:
                active.remove(identity)
            continue
        if type(item) in (dict, list):
            container = cast(dict[str, object] | list[object], item)
            if id(container) in active:
                raise _invalid("cyclic JSON containers are not supported")
            if depth >= _MAX_DEPTH:
                raise _invalid("JSON depth limit exceeded")
            size += 2 + max(0, len(container) - 1)
            if size > max_bytes:
                raise _invalid("JSON size limit exceeded")
            if type(container) is dict:
                mapping = cast(dict[str, object], container)
                for key in mapping:
                    if type(key) is not str:
                        raise _invalid("JSON object keys must be strings")
                    size += _scalar_size(key, max_bytes) + 1
                    if size > max_bytes:
                        raise _invalid("JSON size limit exceeded")
                children = iter(mapping.values())
            else:
                children = iter(container)
            active.add(id(container))
            stack.append((children, depth + 1, id(container)))
        else:
            size += _scalar_size(item, max_bytes)
        if size > max_bytes:
            raise _invalid("JSON size limit exceeded")


def _text(value: object, *, limit: int = 256) -> str:
    if type(value) is not str or not 1 <= len(value) <= limit or not value.strip():
        raise _invalid("expected a bounded nonblank string")
    return value


def _node_id(value: object) -> str:
    value = _text(value, limit=64)
    if _NODE_ID.fullmatch(value) is None:
        raise _invalid("node IDs must be slugs")
    return value


def _allowlist(activities: Collection[tuple[str, str]]) -> frozenset[tuple[str, str]]:
    if not isinstance(activities, Collection):
        raise _invalid("allowed_activities must be a collection of name/version pairs")
    result: set[tuple[str, str]] = set()
    for pair in activities:
        if type(pair) not in (tuple, list) or len(pair) != 2:
            raise _invalid("allowed_activities must contain name/version pairs")
        result.add((_text(pair[0]), _text(pair[1])))
    return frozenset(result)


def _matcher(value: JsonValue, *, nonempty: bool = False) -> None:
    if type(value) is not dict or len(value) > 32 or (nonempty and not value):
        raise _invalid("matchers must be bounded flat objects; completion_match cannot be empty")
    for key, scalar in value.items():
        _text(key)
        if type(scalar) in (dict, list):
            raise _invalid("matchers must contain only JSON scalars")
    _check_json(value, max_bytes=4096)


def _edges(node: JsonObject) -> tuple[str, ...]:
    if node["type"] == "complete":
        return ()
    if node["type"] == "wait":
        return (cast(str, node["next"]),)
    return (cast(str, node["next"]), cast(str, node["on_failure"]))


def validate_graph(
    graph: object, *, allowed_activities: Collection[tuple[str, str]]
) -> JsonObject:
    """Validate schema v1 and return an independent copy; raise safe ValueError on failure."""
    _check_json(graph)
    if type(graph) is not dict or set(graph) != {"schema_version", "entry", "nodes"}:
        raise _invalid("graph must contain exactly schema_version, entry and nodes")
    graph = cast(JsonObject, graph)
    if type(graph["schema_version"]) is not int or graph["schema_version"] != 1:
        raise _invalid("unsupported schema_version")
    entry = _node_id(graph["entry"])
    nodes = graph["nodes"]
    if type(nodes) is not dict or not 2 <= len(nodes) <= _MAX_NODES:
        raise _invalid("graph must contain 2 to 64 nodes")
    allowed = _allowlist(allowed_activities)
    adjacency: dict[str, tuple[str, ...]] = {}
    for node_id, node in nodes.items():
        _node_id(node_id)
        if type(node) is not dict:
            raise _invalid("nodes must be objects")
        kind = node.get("type")
        if kind == "wait":
            required = {"type", "signal", "next"}
            optional = {"match"}
        elif kind == "activity":
            required = {"type", "activity", "version", "input", "next", "on_failure"}
            optional = _DEFERRED
        elif kind == "complete":
            required = {"type", "result"}
            optional = set()
        else:
            raise _invalid("unsupported node type")
        if not required <= node.keys() or node.keys() - required - optional:
            raise _invalid("node has missing or unsupported fields")
        if kind == "wait":
            _text(node["signal"])
            if "match" in node:
                _matcher(node["match"])
        elif kind == "activity":
            identity = (_text(node["activity"]), _text(node["version"]))
            if identity not in allowed:
                raise _invalid("activity name/version is not allowed")
            if type(node["input"]) is not dict:
                raise _invalid("activity input must be a JSON object")
            deferred = node.keys() & _DEFERRED
            if deferred and deferred != _DEFERRED:
                raise _invalid("completion fields must be supplied together")
            if deferred:
                _text(node["completion_signal"])
                _matcher(node["completion_match"], nonempty=True)
                _matcher(node["completion_success"])
        adjacency[node_id] = tuple(_node_id(target) for target in _edges(node))
    if entry not in nodes or any(
        target not in nodes for edges in adjacency.values() for target in edges
    ):
        raise _invalid("entry and edges must reference existing nodes")

    # At most 64 nodes, so this DFS is bounded independently of JSON nesting.
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise _invalid("graph must be acyclic, including failure edges")
        if node_id in visited:
            return
        visiting.add(node_id)
        for target in adjacency[node_id]:
            visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(entry)
    if visited != nodes.keys():
        raise _invalid("all nodes must be reachable from entry")
    # Only complete nodes have no outgoing edges; an acyclic graph therefore terminates.
    return deepcopy(graph)


def parse_graph(
    text: str, *, allowed_activities: Collection[tuple[str, str]]
) -> JsonObject:
    """Parse bounded UTF-8 JSON, rejecting duplicate keys and excessive depth before decoding."""
    if type(text) is not str or len(text) > _MAX_BYTES:
        raise _invalid("JSON text size limit exceeded")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise _invalid("JSON text must be valid UTF-8") from None
    if size > _MAX_BYTES:
        raise _invalid("JSON text size limit exceeded")
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _MAX_DEPTH:
                raise _invalid("JSON depth limit exceeded")
        elif char in "]}":
            depth -= 1

    def object_pairs(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
        result: JsonObject = {}
        for key, value in pairs:
            if key in result:
                raise _invalid("duplicate JSON object key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> JsonValue:
        raise _invalid("numbers must be finite")

    try:
        graph = json.loads(text, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise _invalid("invalid JSON text, duplicate keys or nonfinite numbers") from None
    return validate_graph(graph, allowed_activities=allowed_activities)


def _matches(expected: JsonObject, body: object) -> bool:
    # No recursive event traversal or copying, and no truthiness (True must not equal 1).
    if not expected:
        return True
    if type(body) is not dict:
        return False
    return all(
        key in body and type(body[key]) is type(value) and body[key] == value
        for key, value in expected.items()
    )


@dataclass(frozen=True)
class DeclarativeWorkflow:
    """Reusable WorkflowDefinition for explicit, allowlisted schema-v1 graphs."""

    name: str = "ace.declarative"
    version: str = "1"
    allowed_activities: Collection[tuple[str, str]] = field(kw_only=True)

    def __post_init__(self) -> None:
        _text(self.name)
        _text(self.version)
        object.__setattr__(self, "allowed_activities", _allowlist(self.allowed_activities))

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        if type(input) is not dict or set(input) != {"graph"}:
            raise _invalid("start input must contain exactly graph")
        graph = validate_graph(input["graph"], allowed_activities=self.allowed_activities)
        return self._enter({"graph": graph, "node": graph["entry"], "completed": []})

    def advance(
        self, state: JsonObject, event: WorkflowEvent, context: WorkflowContext
    ) -> WorkflowTransition:
        state = self._copy_state(state)
        if event.event_type == WorkflowEventType.CANCELLATION_REQUESTED:
            return WorkflowTransition(state, (CancelWorkflow(reason="Cancellation requested"),))
        nodes = cast(dict[str, JsonObject], cast(JsonObject, state["graph"])["nodes"])
        node_id = cast(str, state["node"])
        node = nodes[node_id]
        target = None
        payload = event.payload if type(event.payload) is dict else {}
        activity_key = payload.get("activity_key")
        if event.event_type == WorkflowEventType.SIGNAL_RECEIVED:
            body = payload.get("payload")
            signal = payload.get("signal_name")
            if node["type"] == "wait" and type(signal) is str and signal == node["signal"]:
                if _matches(cast(JsonObject, node.get("match", {})), body):
                    target = node["next"]
            elif node["type"] == "activity" and "completion_signal" in node:
                if (
                    type(signal) is str
                    and signal == node["completion_signal"]
                    and _matches(cast(JsonObject, node["completion_match"]), body)
                ):
                    success = _matches(cast(JsonObject, node["completion_success"]), body)
                    target = node["next"] if success else node["on_failure"]
        elif node["type"] == "activity" and type(activity_key) is str and activity_key == node_id:
            if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:
                if "completion_signal" not in node:
                    target = node["next"]
            elif event.event_type in (
                WorkflowEventType.ACTIVITY_FAILED,
                WorkflowEventType.ACTIVITY_CANCELLED,
            ):
                target = node["on_failure"]
            elif event.event_type == WorkflowEventType.ACTIVITY_TIMED_OUT:
                # ACE Django retries attempts internally and emits ACTIVITY_FAILED on
                # exhaustion. Other adapters must explicitly mark a timeout as final.
                if payload.get("final") is True:
                    target = node["on_failure"]
        if target is None:
            return WorkflowTransition(state, status=WorkflowStatus.WAITING)
        cast(list[JsonValue], state["completed"]).append(node_id)
        state["node"] = target
        return self._enter(state)

    def _copy_state(self, state: JsonObject) -> JsonObject:
        if type(state) is not dict or set(state) != {"graph", "node", "completed"}:
            raise _invalid("state must contain exactly graph, node and completed")
        graph = validate_graph(state["graph"], allowed_activities=self.allowed_activities)
        nodes = cast(dict[str, JsonObject], graph["nodes"])
        node_id = _node_id(state["node"])
        completed = state["completed"]
        if node_id not in nodes or type(completed) is not list or len(completed) > _MAX_NODES:
            raise _invalid("invalid current node or completed path")
        if any(type(item) is not str or item not in nodes for item in completed):
            raise _invalid("invalid completed node")
        if len(set(completed)) != len(completed):
            raise _invalid("completed nodes cannot repeat")
        trail = cast(list[str], completed).copy()
        if nodes[node_id]["type"] != "complete" or not trail or trail[-1] != node_id:
            if node_id in trail:
                raise _invalid("current node already completed")
            trail.append(node_id)
        if trail[0] != graph["entry"] or any(
            right not in _edges(nodes[left]) for left, right in zip(trail, trail[1:])
        ):
            raise _invalid("state must follow a path from entry")
        return {"graph": graph, "node": node_id, "completed": completed.copy()}

    def _enter(self, state: JsonObject) -> WorkflowTransition:
        nodes = cast(dict[str, JsonObject], cast(JsonObject, state["graph"])["nodes"])
        node_id = cast(str, state["node"])
        node = nodes[node_id]
        if node["type"] == "wait":
            return WorkflowTransition(state, status=WorkflowStatus.WAITING)
        if node["type"] == "complete":
            cast(list[JsonValue], state["completed"]).append(node_id)
            return WorkflowTransition(state, (CompleteWorkflow(result=deepcopy(node["result"])),))
        return WorkflowTransition(
            state=state,
            commands=(ScheduleActivity(
                activity_key=node_id,
                activity_name=cast(str, node["activity"]),
                activity_version=cast(str, node["version"]),
                input=deepcopy(cast(JsonObject, node["input"])),
            ),),
        )