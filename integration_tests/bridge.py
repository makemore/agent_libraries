"""HTTPConnection bridge: real Django REST dispatch, no TCP/TLS simulation."""

from contextlib import ExitStack
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.db import connections
from django.test import TestCase
from rest_framework.test import APIClient


def read_only_sql(execute, sql, params, many, context):
    # Do not retain SQL/parameters: token authentication includes a credential.
    # Fail closed on unknown statements (including write CTEs); atomic read
    # endpoints may legitimately use transaction/savepoint statements.
    command = sql.lstrip().split(None, 1)[0].upper()
    if command not in {"SELECT", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"}:
        raise AssertionError("Adapter read attempted a non-read SQL statement")
    return execute(sql, params, many, context)


class DjangoBridge:
    """Only replace socket transport; preserve Client headers and JSON decoding."""

    def __init__(self):
        self.requests = []  # Method/path/query only. Never record request headers.

    def request(self, connection, method, target, body=None, headers=None):
        __tracebackhide__ = True
        parsed = urlsplit(target)
        if (connection.host, connection.port) != ("127.0.0.1", 80) or parsed.netloc or parsed.scheme:
            raise AssertionError("REST bridge only accepts its private test origin")
        if method != "GET" or body is not None:
            raise AssertionError("Read adapter attempted a non-GET request")
        self.requests.append(("GET", parsed.path, parsed.query))
        headers = {"HTTP_" + key.upper().replace("-", "_"): value
                   for key, value in (headers or {}).items()}
        headers["HTTP_HOST"] = "127.0.0.1"
        with ExitStack() as stack:
            for database in connections.all():
                stack.enter_context(database.execute_wrapper(read_only_sql))
            # Execute deferred callbacks while guards are active, not at teardown.
            stack.enter_context(TestCase.captureOnCommitCallbacks(execute=True))
            response = APIClient().get(target, **headers)
        # Hand the actual REST status, headers and bytes to Client's normal
        # error handling, content checks and JSON decoder; no fabricated JSON.
        body_stream = BytesIO(response.content)
        return SimpleNamespace(
            status=response.status_code, getheader=response.get,
            read=body_stream.read, close=body_stream.close,
        )
