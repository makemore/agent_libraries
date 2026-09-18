"""Metadata-only adapters for existing, read-only REST operations.

Run detail GETs are deliberately absent: the runtime can dispatch on retrieve.
Neither these projections nor status observations imply authorization grants.
"""

import math
import re
from urllib.parse import quote

from .errors import CLIError
from .transport import validate_mount

_FIELDS = {
    ("runtime", "agents"): (
        "id", "slug", "name", "is_active", "active_version",
    ),
    ("runtime", "runs"): (
        "id", "conversation_id", "agent_key", "status", "attempt", "max_attempts",
        "created_at", "started_at", "finished_at", "superseded_by_id",
        "cancel_requested_at", "suspended_at",
    ),
    ("studio", "projects"): (
        "id", "name", "organization_id", "organization_name", "can_manage",
        "can_create_thread", "revision", "archived",
    ),
    ("studio", "threads"): (
        "id", "project_id", "title", "agent_key", "conversation_id", "visibility",
        "archived", "created_at", "updated_at", "can_write", "can_manage",
    ),
}
_IDENTITY_FIELDS = (
    "id", "username", "email", "full_name", "preferred_name", "is_active",
)
_QUERY_FIELDS = {
    ("runtime", "agents"): {"system", "page", "page_size"},
    ("runtime", "runs"): {"agent_key", "page", "page_size"},
    ("studio", "projects"): set(),
    ("studio", "threads"): {"project_id", "archived", "limit", "offset"},
}
_MAX_INTEGER = 2147483647


def _unsupported():
    raise CLIError("This read operation is not configured or supported.",
                   code="unsupported", exit_code=4)


def _invalid_response():
    raise CLIError("The server returned an invalid metadata response.",
                   code="protocol", exit_code=6) from None


def _invalid_pagination():
    raise CLIError("The server returned invalid or non-progressing pagination.",
                   code="pagination", exit_code=6) from None


def _configured_path(value, *, mount=False):
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CLIError("Configured API paths must be safe absolute URL paths.")
    # Identity endpoints may deliberately lack a trailing slash. Validate a
    # mount-shaped copy without changing the path actually requested.
    validate_mount(value if mount or value.endswith("/") else value + "/")
    return value


def _identifier(value, *, slug=False):
    pattern = r"[A-Za-z0-9_-]{1,255}" if slug else r"[A-Za-z0-9._:-]{1,255}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or value in {".", ".."}:
        raise CLIError("A safe resource identifier is required.")
    return value


def _integer(value, minimum, maximum=_MAX_INTEGER):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise CLIError("A query or page bound is outside its supported integer range.")
    return value


def _query(key, query):
    if query is None:
        return {}
    if not isinstance(query, dict) or query.keys() - _QUERY_FIELDS[key]:
        raise CLIError("Unsupported query options for this resource.")
    result = {}
    for name, value in query.items():
        if name in {"page", "page_size", "limit", "offset"}:
            result[name] = _integer(value, 0 if name == "offset" else 1,
                                    100 if name == "limit" else _MAX_INTEGER)
        elif name == "archived":
            if type(value) is bool:
                value = "true" if value else "false"
            if not isinstance(value, str) or value not in {"true", "false"}:
                raise CLIError("The archived filter must be true or false.")
            result[name] = value
        elif name in {"system", "project_id"}:
            result[name] = _identifier(value, slug=name == "system")
        else:
            if (not isinstance(value, str) or not 1 <= len(value) <= 255
                    or not all(char.isprintable() for char in value)):
                raise CLIError("A bounded, printable filter value is required.")
            result[name] = value
    return result


def _camel_case(name):
    first, *rest = name.split("_")
    return first + "".join(part.title() for part in rest)


class ReadAPI:
    def __init__(self, client, *, runtime_mount=None, studio_mount=None, identity_path=None):
        self.client = client
        self.runtime_mount = _configured_path(runtime_mount, mount=True)
        self.studio_mount = _configured_path(studio_mount, mount=True)
        self.identity_path = _configured_path(identity_path)

    def _path(self, group, resource):
        if (not isinstance(group, str) or not isinstance(resource, str)
                or (group, resource) not in _FIELDS):
            _unsupported()
        mount = self.runtime_mount if group == "runtime" else self.studio_mount
        if mount is None:
            _unsupported()
        suffix = resource if group == "runtime" else "workspace/" + resource
        return mount.rstrip("/") + "/" + suffix + "/"

    def _safe_text(self, value):
        # Replace rather than delete controls, so separated text cannot turn
        # into an active credential after sanitization.
        value = self.client.redact(value)
        value = "".join(char if char.isprintable() else " " for char in value)
        return self.client.redact(value)

    def _project(self, row, fields, *, camel=False):
        if not isinstance(row, dict):
            _invalid_response()
        identifier = row.get("id")
        if type(identifier) not in {str, int} or identifier == "":
            _invalid_response()
        result = {}
        for field in fields:
            alias = _camel_case(field) if camel else field
            if field not in row and alias not in row:
                continue
            if field != alias and field in row and alias in row and row[field] != row[alias]:
                _invalid_response()
            value = row[field] if field in row else row[alias]
            if value is not None and type(value) not in {str, int, float, bool}:
                _invalid_response()
            if isinstance(value, float) and not math.isfinite(value):
                _invalid_response()
            if isinstance(value, str):
                value = self._safe_text(value)
            result[field] = value
        return result

    def _runtime_next(self, key, url, path, current):
        # The transport validates origin AND exact resource path before any GET.
        # Never request the supplied URL directly, even for a default single page.
        try:
            following = _query(key, self.client.next_query(url, path))
        except CLIError:
            _invalid_pagination()
        if following.get("page", 1) != current.get("page", 1) + 1:
            _invalid_pagination()
        if ({k: v for k, v in following.items() if k != "page"}
                != {k: v for k, v in current.items() if k != "page"}):
            _invalid_pagination()
        return following

    def list(self, group, resource, *, query=None, all_pages=False, max_pages=10) -> dict:
        path = self._path(group, resource)
        key = (group, resource)
        current = _query(key, query)
        if type(all_pages) is not bool or type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise CLIError("Pagination requires a boolean all_pages and max_pages from 1 to 100.")
        items, seen_ids, seen_queries = [], set(), set()
        total = shape = None
        for pages in range(1, max_pages + 1):
            signature = tuple(sorted(current.items()))
            if signature in seen_queries:
                _invalid_pagination()
            seen_queries.add(signature)
            payload = self.client.get(path, query=current or None)
            following = None
            flat = isinstance(payload, list)
            if shape is not None and shape != flat:
                _invalid_pagination()
            shape = flat
            if flat and key != ("studio", "threads"):
                rows, count, has_more = payload, len(payload), False
            elif isinstance(payload, dict) and key != ("studio", "projects"):
                rows, count = payload.get("results"), payload.get("count")
                if not isinstance(rows, list) or type(count) is not int or count < len(rows):
                    _invalid_response()
                if group == "runtime":
                    if "next" not in payload:
                        _invalid_response()
                    next_url = payload["next"]
                    if next_url is not None and (not isinstance(next_url, str) or not next_url):
                        _invalid_pagination()
                    has_more = next_url is not None
                    if has_more:
                        following = self._runtime_next(key, next_url, path, current)
                else:
                    has_more = payload.get("has_more")
                    offset = current.get("offset", 0)
                    end = offset + len(rows)
                    if (type(has_more) is not bool or len(rows) > current.get("limit", 100)
                            or (rows and end > count) or has_more != (end < count)):
                        _invalid_pagination()
                    if has_more:
                        if end > _MAX_INTEGER:
                            _invalid_pagination()
                        following = {**current, "offset": end}
            else:
                _invalid_response()
            if total is not None and count != total:
                _invalid_pagination()
            total = count
            # Runtime retention filtering happens after pagination. An entire
            # run page can be omitted while a later page remains readable.
            if len(items) + len(rows) > total or (has_more and not rows and key != ("runtime", "runs")):
                _invalid_pagination()
            for row in rows:
                projected = self._project(row, _FIELDS[key], camel=group == "runtime")
                # Compare source IDs, not redacted IDs (which can collide).
                identity = (type(row["id"]), row["id"])
                if identity in seen_ids:
                    _invalid_pagination()
                seen_ids.add(identity)
                items.append(projected)
            if has_more and group == "runtime" and len(items) >= total:
                _invalid_pagination()
            # Runtime counts can include retained/expired rows omitted by list;
            # a terminal page need not bring the projected total up to count.
            if not all_pages or not has_more:
                return {"items": items, "count": total, "has_more": has_more, "pages": pages}
            if pages == max_pages:
                raise CLIError("Pagination reached max_pages before completion; no partial result returned.",
                               code="pagination", exit_code=6)
            current = following

    def get(self, group, resource, identifier) -> dict:
        path = self._path(group, resource)
        if (group, resource) == ("runtime", "runs"):
            _unsupported()
        segment = quote(_identifier(identifier, slug=group == "runtime"), safe="")
        payload = self.client.get(path + segment + "/")
        return self._project(payload, _FIELDS[(group, resource)], camel=group == "runtime")

    def whoami(self) -> dict:
        if self.identity_path is None:
            _unsupported()
        payload = self.client.get(self.identity_path)
        if not isinstance(payload, dict):
            _invalid_response()
        payload = dict(payload)
        if payload.get("id") is None:
            payload["id"] = payload.get("pk")
        return self._project(payload, _IDENTITY_FIELDS, camel=True)

    def status(self) -> dict:
        """At most three GETs; observations are not capability/auth guarantees."""
        probes = []
        if self.runtime_mount is not None:
            probes.append(("runtime", "agents"))
        if self.studio_mount is not None:
            probes.append(("studio", "projects"))
        if self.identity_path is not None:
            probes.append(("identity", "whoami"))
        if not probes:
            _unsupported()
        observations = []
        for group, resource in probes:
            observation = {"group": group, "resource": resource}
            try:
                if group == "identity":
                    self.whoami()
                else:
                    self.list(group, resource, query={"page_size": 1} if group == "runtime" else None)
            except CLIError as exc:
                observation["availability"] = "observed_unavailable"
                if type(exc.status) is int and 100 <= exc.status <= 599:
                    observation["status"] = exc.status
            else:
                observation["availability"] = "observed_available"
            observations.append({key: self._safe_text(value) if isinstance(value, str) else value
                                 for key, value in observation.items()})
        return {"observations": observations,
                "scope": self._safe_text("Observed read availability only; not capability or authorization grants.")}
