#!/usr/bin/env python3
"""Fail-closed, loopback-only projection of Bifrost's virtual-key model catalog.

Run with Python 3.11, no arguments, environment configuration or dependencies.
No provider/admin credentials are loaded, cached, or used. The caller's VK is
used only for this request; token syntax is NOT authentication. In the pinned
empty deployment even an invalid VK can receive HTTP 200 with data: [], so ALL
empty catalogs return 403, including valid keys with no grants/models.

Trust: nonempty data must be governance-scoped by the pinned Bifrost image
sha256:a8942692af7b4b89196cd8fc33653b7353488dfd58b24078fe793b8574a8084b.
That property requires a separate isolated container proof (see the gateway's
tests/test_model_discovery.py); these offline unit tests do not authenticate
VKs or prove upstream scoping. Gate every image upgrade on that proof, covering
cross-key isolation, revoked/invalid keys and empty deployments. Never bypass
the empty-list denial to make the empty-deployment authentication proof pass.

Caddy integration (not installed by this program): in the public site insert
the following before the existing inference handler, retaining its final 404
handler and management denial. Do NOT import keyed_inference for this route:

    @model_catalog {
        method GET
        path /v1/models
    }
    handle @model_catalog {
        reverse_proxy 127.0.0.1:9092
    }

Keep Caddy access logging disabled, its error-header/URI redaction and -Server
policy, and ports 8080/9092 private in the same network namespace. Do not enable
this route until the pinned-image scoping proof passes. A disposable Caddy test
config can use the same handler in a loopback-only site with a final 404; never
open management paths or change provider/storage defaults for that test.

Offline tests: .venv/bin/python -B -m unittest discover -s tests
              -p test_model_catalog.py -v
"""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import sys
import threading


LISTEN_ADDRESS = ("127.0.0.1", 9092)
UPSTREAM_ADDRESS = ("127.0.0.1", 8080)
MODELS_PATH = "/v1/models"
NETWORK_TIMEOUT = 10
SOCKET_TIMEOUT = 5
MAX_WORKERS = 16
MAX_AUTH_BYTES = 4096
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ROWS = 10000
AUTHORIZATION = re.compile(r"Bearer sk-bf-[A-Za-z0-9_-]+", re.ASCII)
# Slash-separated model/provider segments with an optional model version/tier.
# No whitespace, controls, URL escaping, authority, query, or fragment syntax.
MODEL_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*)?", re.ASCII,
)
ERROR_BODIES = {
    400: b'{"error":{"message":"Bad request"}}',
    401: b'{"error":{"message":"Virtual key required"}}',
    403: b'{"error":{"message":"Catalog access denied"}}',
    404: b'{"error":{"message":"Not found"}}',
    502: b'{"error":{"message":"Catalog unavailable"}}',
}


class CatalogDenied(Exception):
    """No authorized, nonempty catalog; never attach upstream details."""


def valid_authorization(value):
    return (isinstance(value, str) and len(value) <= MAX_AUTH_BYTES
            and AUTHORIZATION.fullmatch(value) is not None)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid catalog")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Invalid catalog")


def project_catalog(body):
    """Validate every row before releasing anything; discard all diagnostics."""
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Invalid catalog")
    document = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        raise ValueError("Invalid catalog")
    rows = document["data"]
    if not rows:
        raise CatalogDenied()
    if (len(rows) > MAX_ROWS or document.get("object", "list") != "list"
            or "error" in document or "errors" in document):
        raise ValueError("Invalid catalog")
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("object", "model") != "model":
            raise ValueError("Invalid catalog")
        identifier = row.get("id")
        if (not isinstance(identifier, str) or len(identifier) > 255
                or MODEL_ID.fullmatch(identifier) is None):
            raise ValueError("Invalid catalog")
        lowered = identifier.lower()
        # Do not let an echoed bearer/VK/common sk-* credential become an ID.
        # Arbitrary opaque secrets cannot be recognized without knowing them;
        # upstream ID authenticity remains part of the pinned-image contract.
        if ("bearer" in lowered or "sk-" in lowered
                or lowered.startswith(("http:", "https:", "ftp:", "file:",
                                       "data:", "javascript:", "mailto:", "urn:"))):
            raise ValueError("Invalid catalog")
        if identifier not in seen:
            seen.add(identifier)
            models.append({"id": identifier, "object": "model",
                           "owned_by": identifier.split("/", 1)[0] if "/" in identifier else ""})
    result = json.dumps({"object": "list", "data": models}, separators=(",", ":")).encode("ascii")
    if len(result) > MAX_RESPONSE_BYTES:
        raise ValueError("Invalid catalog")
    return result


def fetch_catalog(authorization):
    """One fixed HTTP request, no proxy discovery, redirects, cookies or retry."""
    if not valid_authorization(authorization):
        raise ValueError("Invalid authorization")
    connection = http.client.HTTPConnection(*UPSTREAM_ADDRESS, timeout=NETWORK_TIMEOUT)
    try:
        # HTTPConnection adds only the required fixed Host header. Unlike urllib
        # it has no environment proxies, redirects, auth handlers or cookie jar.
        connection.request("GET", MODELS_PATH, headers={
            "Authorization": authorization,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        })
        with connection.getresponse() as response:
            if response.status in (401, 403):
                raise CatalogDenied()
            if response.status != 200:
                raise ValueError("Invalid catalog")
            encodings = response.headers.get_all("Content-Encoding", [])
            if encodings and (len(encodings) != 1 or encodings[0].strip().lower() != "identity"):
                raise ValueError("Invalid catalog")
            types = response.headers.get_all("Content-Type", [])
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Invalid catalog")
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            if transfers and (lengths or len(transfers) != 1 or transfers[0].lower() != "chunked"):
                raise ValueError("Invalid catalog")
            expected = None
            if lengths:
                if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,10}", lengths[0]) is None:
                    raise ValueError("Invalid catalog")
                expected = int(lengths[0])
                if expected > MAX_RESPONSE_BYTES:
                    raise ValueError("Invalid catalog")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES or (expected is not None and len(body) != expected):
                raise ValueError("Invalid catalog")
            return body
    finally:
        connection.close()


def catalog_response(authorization, fetch):
    """The only error boundary: never serialize an exception or upstream body."""
    if not valid_authorization(authorization):
        return 401, ERROR_BODIES[401]
    try:
        return 200, project_catalog(fetch(authorization))
    except CatalogDenied:
        return 403, ERROR_BODIES[403]
    except Exception:
        return 502, ERROR_BODIES[502]


class CatalogHandler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT
    server_version = ""
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def log_error(self, format, *args):
        pass

    def log_request(self, code="-", size="-"):
        pass

    def handle(self):
        try:
            super().handle()
        except Exception:
            # Disconnected/timed-out clients must not invoke traceback logging.
            pass

    def _respond(self, status, body):
        self.close_connection = True
        # send_response_only omits both the server signature and automatic logs.
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # Parser errors and unsupported methods never reflect the request line.
        status = 404 if code == 501 else 400
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"
        self._respond(status, ERROR_BODIES[status])

    def do_GET(self):
        # BaseHTTPRequestHandler normalizes //; also inspect the original target.
        target = self.requestline.split()[1]
        if self.request_version not in ("HTTP/1.0", "HTTP/1.1"):
            self.send_error(400)
            return
        if target != MODELS_PATH or self.path != MODELS_PATH:
            status = 400 if target.startswith(MODELS_PATH + "?") else 404
            self._respond(status, ERROR_BODIES[status])
            return
        # No bodies, upgrades, interim responses or pipelined follow-up requests.
        lengths = self.headers.get_all("Content-Length", [])
        if (lengths not in ([], ["0"]) or "Transfer-Encoding" in self.headers
                or "Expect" in self.headers or "Upgrade" in self.headers):
            self._respond(400, ERROR_BODIES[400])
            return
        values = self.headers.get_all("Authorization", [])
        authorization = values[0] if len(values) == 1 else None
        status, body = catalog_response(authorization, self.server.fetch)
        self._respond(status, body)


class CatalogServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_WORKERS

    def __init__(self, fetch=None):
        self.fetch = fetch_catalog if fetch is None else fetch
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)
        super().__init__(LISTEN_ADDRESS, CatalogHandler)

    def get_request(self):
        request, address = super().get_request()
        try:
            request.settimeout(SOCKET_TIMEOUT)
        except Exception:
            request.close()
            raise
        return request, address

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        except Exception:
            pass

    def process_request(self, request, client_address):
        # Acquire BEFORE creating a thread; excess connections are closed, not
        # assigned unbounded waiting threads. The OS listen backlog is also 16.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        except Exception:
            pass
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # socketserver's default would print request details and a traceback.
        pass


def main():
    try:
        with CatalogServer() as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() if len(sys.argv) == 1 else 1)