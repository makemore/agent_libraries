"""GET-only JSON transport using stdlib direct HTTP(S) connections.

HTTPS uses the default verified TLS context. Only an explicitly enabled literal
loopback/localhost origin may use HTTP. There is no proxy, netrc, cookie, redirect,
or ambient authentication support. Timeout is a finite socket timeout in (0,120]
seconds; response bodies are limited to 4 MiB. No response body, URL, header or
underlying exception text is included in diagnostics. Successful JSON is returned
to the resource adapter, which remains responsible for its output allowlist.

Pagination accepts absolute same-origin URLs, root-relative URLs, or query-only
references, always for the exact requested resource path. Only page, page_size,
limit, offset, cursor, system, agent_key, project_id and archived are allowed,
with no duplicate keys.
These restrictions apply to get() query mappings too. redact() replaces every
exact occurrence of the active credential, without heuristic secret detection.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit

from .config import _validate_path, _validate_token, validate_mount, validate_origin
from .errors import CLIError

__all__ = ["Client", "validate_mount", "validate_origin"]

_RESPONSE_LIMIT = 4 * 1024 * 1024
_QUERY_KEYS = frozenset({
    "page", "page_size", "limit", "offset", "cursor", "system", "agent_key",
    "project_id", "archived",
})


def _protocol_error():
    return CLIError("Invalid server response or request target.", code="protocol", exit_code=6)


def _http_error(status):
    if status in {401, 403}:
        return CLIError("Authentication or permission denied.", code="auth", exit_code=3, status=status)
    if status == 404:
        return CLIError("Resource not found.", code="not_found", exit_code=4, status=status)
    return CLIError("HTTP request failed.", code="http", exit_code=7, status=status)


def _path(path):
    try:
        # Encoded colons are safe within opaque workspace IDs. No other escape
        # is accepted: in particular encoded slashes/dot segments remain denied.
        checked = re.sub("%3[aA]", "-", path) if isinstance(path, str) else path
        _validate_path(checked)
        return path
    except CLIError:
        raise _protocol_error() from None


def _query(query):
    if not isinstance(query, dict) or not query.keys() <= _QUERY_KEYS:
        raise _protocol_error()
    result = {}
    for key, value in query.items():
        if type(value) not in (str, int):
            raise _protocol_error()
        try:
            text = str(value)
        except ValueError:
            raise _protocol_error() from None
        if len(text) > 8192 or (text and not text.isprintable()):
            raise _protocol_error()
        result[key] = text
    try:
        if len(urlencode(result)) > 8192:
            raise _protocol_error()
    except UnicodeError:
        raise _protocol_error() from None
    return result


def _reject_constant(value):
    raise _protocol_error()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _protocol_error()
        result[key] = value
    return result


def _validate_json(value, depth=0):
    if depth > 64 or (isinstance(value, float) and not math.isfinite(value)):
        raise _protocol_error()
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        _validate_json(child, depth + 1)
    return value


class Client:
    def __init__(self, origin, token, *, scheme="Token", allow_http_loopback=False, timeout=15):
        self.origin = validate_origin(origin, allow_http_loopback=allow_http_loopback)
        self._token = _validate_token(token)
        if scheme not in ("Token", "Bearer"):
            raise CLIError("Invalid authentication scheme.", code="configuration", exit_code=2)
        if (type(timeout) not in (int, float) or not 0 < timeout <= 120
                or not math.isfinite(timeout)):
            raise CLIError("Invalid request timeout.", code="configuration", exit_code=2)
        self.scheme = scheme
        self.timeout = timeout
        self._allow_http_loopback = allow_http_loopback
        self._origin_parts = urlsplit(self.origin)

    def redact(self, text):
        return text.replace(self._token, "[REDACTED]")

    def get(self, path, query=None):
        path = _path(path)
        parameters = _query({} if query is None else query)
        target = path + ("?" + urlencode(parameters) if parameters else "")
        connection = None
        response = None
        try:
            if self._origin_parts.scheme == "https":
                connection = http.client.HTTPSConnection(
                    self._origin_parts.hostname, self._origin_parts.port,
                    timeout=self.timeout, context=ssl.create_default_context(),
                )
            else:
                connection = http.client.HTTPConnection(
                    self._origin_parts.hostname, self._origin_parts.port, timeout=self.timeout,
                )
            connection.request("GET", target, headers={
                "Authorization": f"{self.scheme} {self._token}",
                "Accept": "application/json", "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            status = response.status
            if type(status) is not int or not 100 <= status <= 599:
                raise _protocol_error()
            if not 200 <= status < 300:
                raise _http_error(status)
            media_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if not (media_type == "application/json" or (
                media_type.startswith("application/") and media_type.endswith("+json")
            )):
                raise _protocol_error()
            encoding = response.getheader("Content-Encoding")
            if encoding is not None and encoding.strip().lower() != "identity":
                raise _protocol_error()
            content_length = response.getheader("Content-Length")
            if content_length is not None and (
                not re.fullmatch(r"[0-9]{1,10}", content_length)
                or int(content_length) > _RESPONSE_LIMIT
            ):
                raise _protocol_error()
            body = response.read(_RESPONSE_LIMIT + 1)
            if len(body) > _RESPONSE_LIMIT or (
                content_length is not None and len(body) != int(content_length)
            ):
                raise _protocol_error()
            try:
                return _validate_json(json.loads(
                    body.decode("utf-8"), parse_constant=_reject_constant,
                    object_pairs_hook=_unique_object,
                ))
            except (ValueError, UnicodeError, RecursionError):
                raise _protocol_error() from None
        except (OSError, http.client.HTTPException, ValueError):
            raise CLIError("Request transport failed.", code="transport", exit_code=5) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except (OSError, http.client.HTTPException):
                    pass
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass

    def next_query(self, next_url, path) -> dict:
        path = _path(path)
        if (not isinstance(next_url, str) or not next_url or len(next_url) > 16384
                or not next_url.isascii() or not next_url.isprintable()
                or any(c in next_url for c in "\\# ") or next_url.startswith("//")):
            raise _protocol_error()
        try:
            parts = urlsplit(next_url)
            if parts.scheme or parts.netloc:
                if not parts.scheme or not parts.netloc:
                    raise _protocol_error()
                origin = validate_origin(f"{parts.scheme}://{parts.netloc}", self._allow_http_loopback)
                if origin != self.origin:
                    raise _protocol_error()
                next_path = parts.path
            elif next_url.startswith("?"):
                next_path = path
            elif next_url.startswith("/"):
                next_path = parts.path
            else:
                raise _protocol_error()
            if _path(next_path) != path or not parts.query:
                raise _protocol_error()
            if re.search(r"%(?![0-9A-Fa-f]{2})", parts.query):
                raise _protocol_error()
            pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=len(_QUERY_KEYS))
            if len(dict(pairs)) != len(pairs):
                raise _protocol_error()
            return _query(dict(pairs))
        except (ValueError, UnicodeError, CLIError):
            raise _protocol_error() from None
