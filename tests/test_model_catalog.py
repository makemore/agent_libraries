"""Offline stdlib tests; no live gateway, provider, Docker or secret access.

The scoped responses below are synthetic, not proof of Bifrost authorization.
Keep the separate pinned-image container isolation proof as an upgrade gate.
Run: .venv/bin/python -B -m unittest discover -s tests -p test_model_catalog.py -v
"""

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import runpy
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "infrastructure/ai-gateway/runtime/model-catalog.py"
SPEC = importlib.util.spec_from_file_location("gateway_model_catalog", SCRIPT)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
# Deliberately synthetic syntax fixtures, never loaded from configuration.
AUTH = "Bearer sk-bf-unit_test_only"
MODEL = "isolation-alpha/alpha-one"


def catalog(rows=None, **extra):
    return json.dumps({"object": "list", "data": [{"id": MODEL}] if rows is None else rows,
                       **extra}).encode("utf-8")


class MemorySocket:
    """Exercise the stdlib HTTP parser and serializer without network access."""

    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()
        self.timeout = None

    def makefile(self, mode, buffering=None):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        pass


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for name, value in ([("Content-Type", "application/json")] if headers is None else headers):
            self.headers[name] = value
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # Fail closed if any accidentally unmocked connection is attempted.
        for target in ("socket.create_connection", "socket.socket.connect"):
            patcher = mock.patch(target, side_effect=AssertionError("Network forbidden in offline tests"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fetch = mock.Mock(return_value=catalog())

    def result(self, body, authorization=AUTH):
        return guard.catalog_response(authorization, mock.Mock(return_value=body))

    def assert_fixed(self, result, status):
        self.assertEqual(result[0], status)
        self.assertTrue(result[1] == guard.ERROR_BODIES[status], "Error must be fixed, not reflected")

    def request(self, headers=None, method="GET", target="/v1/models", raw=None):
        if raw is None:
            fields = [("Authorization", AUTH)] if headers is None else headers
            lines = [f"{method} {target} HTTP/1.1", "Host: untrusted.invalid"]
            lines.extend(f"{name}: {value}" for name, value in fields)
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        connection = MemorySocket(raw)
        guard.CatalogHandler(connection, ("127.0.0.1", 12345), SimpleNamespace(fetch=self.fetch))
        self.assertEqual(connection.timeout, 5)
        head, body = bytes(connection.output).split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        status = int(lines[0].split()[1])
        fields = dict(line.split(": ", 1) for line in lines[1:])
        self.assertEqual(set(fields), {"Content-Type", "Content-Length", "Cache-Control",
                                      "X-Content-Type-Options", "Connection"})
        self.assertEqual(fields["Content-Type"], "application/json")
        self.assertEqual(fields["Cache-Control"], "no-store")
        self.assertEqual(fields["X-Content-Type-Options"], "nosniff")
        self.assertEqual(fields["Connection"], "close")
        if method != "HEAD":
            self.assertEqual(int(fields["Content-Length"]), len(body))
        return status, body


class ProjectionTests(OfflineCase):
    def test_scoped_catalogs_are_not_cached_or_combined_across_callers(self):
        other = "Bearer sk-bf-other_unit_test"
        scopes = {AUTH: MODEL, other: "isolation-beta/beta-one"}
        self.fetch.side_effect = lambda auth: catalog([{"id": scopes[auth]}])
        for authorization in (AUTH, other, AUTH):
            status, body = guard.catalog_response(authorization, self.fetch)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"object": "list", "data": [{
                "id": scopes[authorization], "object": "model",
                "owned_by": scopes[authorization].split("/")[0],
            }]})
        self.assertEqual(self.fetch.call_count, 3)

    def test_projection_deduplicates_ids_and_derives_ownership_only_from_prefix(self):
        rows = [{"id": MODEL, "object": "model", "owned_by": "wrong", "name": "wrong"},
                {"id": MODEL}, {"id": "unqualified-model"}, {"id": "bedrock/vendor/model.v2:0"}]
        status, body = self.result(catalog(rows))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"object": "list", "data": [
            {"id": MODEL, "object": "model", "owned_by": "isolation-alpha"},
            {"id": "unqualified-model", "object": "model", "owned_by": ""},
            {"id": "bedrock/vendor/model.v2:0", "object": "model", "owned_by": "bedrock"},
        ]})
        self.assertEqual(self.result(b'{"data":[{"id":"model"}]}')[0], 200)

    def test_empty_success_is_denied_for_invalid_revoked_and_valid_ungranted_keys(self):
        # Pinned Bifrost's empty-deployment 200 is NOT authentication evidence.
        for suffix in ("invalid", "revoked", "valid_no_grants", "valid_no_models"):
            with self.subTest(scenario=suffix):
                self.assert_fixed(self.result(catalog([]), "Bearer sk-bf-" + suffix), 403)
        self.assert_fixed(self.result(catalog([], error="synthetic-private-diagnostic")), 403)

    def test_all_raw_fields_and_diagnostics_are_discarded(self):
        marker = "synthetic-private-diagnostic"
        row = {"id": MODEL, "object": "model", "name": marker, "created": marker,
               "owned_by": AUTH, "credential": marker, "error": marker, "extra_fields": {"key": marker}}
        status, body = self.result(catalog([row], key_statuses=[{"error": marker}],
                                          extra_fields={"error": AUTH}, name=marker))
        self.assertEqual(status, 200)
        self.assertTrue(marker.encode() not in body and AUTH.encode() not in body, "No raw metadata")
        self.assertEqual(set(json.loads(body)), {"object", "data"})
        self.assertEqual(set(json.loads(body)["data"][0]), {"id", "object", "owned_by"})

    def test_json_and_envelope_validation(self):
        bad = [b"", b"not-json", b"\xff", b"null", b"[]", b"{}", b'{"data":null}',
               b'{"data":{}}', b'{"data":false}', b'{"data":[],"data":[]}',
               b'{"data":[{"id":"one","id":"two"}]}', b'{"data":[],"extra":NaN}',
               b'{"data":[],"extra":Infinity}', b'{"data":[],"extra":-Infinity}',
               b'{"data":[{"id":"x"}]} trailing', b"[" * 2000 + b"]" * 2000,
               catalog(object="not-list"), catalog(object=None), catalog(error="private"),
               catalog(errors=[]), catalog(error=None), "not-bytes", None]
        for index, body in enumerate(bad):
            with self.subTest(case=index):
                self.assert_fixed(self.result(body), 502)

    def test_every_row_is_validated_including_later_and_duplicate_rows(self):
        invalid = [None, [], "model", 1, True, {}, {"id": None}, {"id": 1}, {"id": []},
                   {"id": MODEL, "object": None}, {"id": MODEL, "object": "list"},
                   {"id": MODEL, "object": {"error": "private"}}]
        for index, row in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_fixed(self.result(catalog([{"id": MODEL}, row])), 502)

    def test_identifier_ascii_grammar_and_reflection_protection(self):
        invalid = ["", "a" * 256, "model with space", "é", "ｍodel", "model\u2028x",
                   "https://private.invalid/model", "//private.invalid/model", "https:private",
                   "/absolute", "provider//model", "provider/../model", "model?query", "model#hash",
                   "model%2fother", "model\\other", "<script>", "user@host", "model,other",
                   "provider/BeArEr-value", AUTH, AUTH[7:], "provider/" + AUTH[7:],
                   "sk-proj-synthetic", "provider/SK-BF-synthetic", "model\n"]
        invalid.extend("model" + chr(code) for code in (*range(32), 127))
        for index, identifier in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_fixed(self.result(catalog([{"id": identifier}])), 502)
        for identifier in ("a", "a" * 255, "openai/gpt-4.1", "provider/vendor/model_v1:free"):
            self.assertEqual(self.result(catalog([{"id": identifier}]))[0], 200)

    def test_byte_and_row_limits_include_exact_boundaries(self):
        body = catalog()
        body += b" " * (guard.MAX_RESPONSE_BYTES - len(body))
        self.assertEqual(self.result(body)[0], 200)
        self.assert_fixed(self.result(body + b" "), 502)
        rows = [{"id": MODEL}] * guard.MAX_ROWS
        self.assertEqual(self.result(catalog(rows))[0], 200)
        self.assert_fixed(self.result(catalog(rows + [{"id": MODEL}])), 502)
        # Small input can expand past the response cap when ownership is added.
        rows = [{"id": "p" * 240 + "/" + str(index)} for index in range(guard.MAX_ROWS)]
        self.assertLess(len(catalog(rows)), guard.MAX_RESPONSE_BYTES)
        self.assert_fixed(self.result(catalog(rows)), 502)

    def test_unexpected_exceptions_are_fixed_and_silent(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            for failure in (TimeoutError, OSError, ValueError, RuntimeError, RecursionError):
                self.fetch.side_effect = failure("synthetic-private-diagnostic")
                self.assert_fixed(guard.catalog_response(AUTH, self.fetch), 502)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")


class TransportTests(OfflineCase):
    def fetch_response(self, response):
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(guard.http.client, "HTTPConnection", return_value=connection) as factory:
            result = guard.catalog_response(AUTH, guard.fetch_catalog)
        factory.assert_called_once_with("127.0.0.1", 8080, timeout=10)
        connection.close.assert_called_once_with()
        return result, connection

    def test_only_fixed_destination_method_and_application_headers(self):
        result, connection = self.fetch_response(Response(catalog()))
        self.assertEqual(result[0], 200)
        connection.request.assert_called_once_with("GET", "/v1/models", headers={
            "Authorization": AUTH, "Accept": "application/json", "Accept-Encoding": "identity",
        })

    def test_actual_http_wire_has_only_fixed_host_and_three_allowed_headers(self):
        payload = catalog()
        wire = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(payload)).encode() + b"\r\n\r\n" + payload)
        stream = MemorySocket(wire)
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=10)
        connection.sock = stream
        with mock.patch.object(guard.http.client, "HTTPConnection", return_value=connection):
            self.assertEqual(guard.catalog_response(AUTH, guard.fetch_catalog)[0], 200)
        lines = bytes(stream.output).decode("ascii").split("\r\n")
        self.assertEqual(lines[0], "GET /v1/models HTTP/1.1")
        headers = dict(line.split(": ", 1) for line in lines[1:] if line)
        self.assertTrue(headers == {"Host": "127.0.0.1:8080", "Authorization": AUTH,
                                   "Accept": "application/json", "Accept-Encoding": "identity"},
                        "Upstream wire headers must be an exact allowlist")

    def test_actual_chunked_body_is_parsed_but_truncated_chunks_are_denied(self):
        payload = catalog()
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        chunk = format(len(payload), "x").encode() + b"\r\n" + payload + b"\r\n"
        for ending, expected in ((b"0\r\n\r\n", 200), (b"", 502)):
            connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=10)
            connection.sock = MemorySocket(head + chunk + ending)
            with mock.patch.object(guard.http.client, "HTTPConnection", return_value=connection):
                result = guard.catalog_response(AUTH, guard.fetch_catalog)
            self.assertEqual(result[0], expected)
            if expected != 200:
                self.assert_fixed(result, expected)

    def test_upstream_auth_statuses_denied_and_all_other_non_200_are_gateway_errors(self):
        for code in (201, 204, 206, 301, 302, 303, 307, 308, 400, 401, 403, 404, 429, 500, 503):
            response = Response(b"synthetic-private-diagnostic", status=code,
                                headers=[("Location", "https://private.invalid/"), ("Set-Cookie", "private")])
            with self.subTest(status=code):
                result, connection = self.fetch_response(response)
                self.assert_fixed(result, 403 if code in (401, 403) else 502)
                self.assertEqual(response.read_sizes, [])
                self.assertEqual(connection.request.call_count, 1)

    def test_identity_only_json_and_unambiguous_response_framing(self):
        base = [("Content-Type", "application/json")]
        invalid = [[], [("Content-Type", "text/html")], base * 2,
                   base + [("Content-Encoding", "gzip")], base + [("Content-Encoding", "br")],
                   base + [("Content-Encoding", "identity, gzip")], base + [("Content-Encoding", "")],
                   base + [("Content-Encoding", "identity")] * 2,
                   base + [("Content-Length", str(guard.MAX_RESPONSE_BYTES + 1))],
                   base + [("Content-Length", "-1")], base + [("Content-Length", "invalid")],
                   base + [("Content-Length", "10")] * 2,
                   base + [("Transfer-Encoding", "gzip, chunked")],
                   base + [("Transfer-Encoding", "chunked")] * 2,
                   base + [("Transfer-Encoding", "chunked"), ("Content-Length", "10")]]
        for index, headers in enumerate(invalid):
            with self.subTest(case=index):
                response = Response(catalog(), headers=headers)
                self.assert_fixed(self.fetch_response(response)[0], 502)
                self.assertEqual(response.read_sizes, [])
        for headers in (base, base + [("Content-Encoding", "identity")],
                        [("Content-Type", "application/json; charset=utf-8")],
                        base + [("Content-Length", str(len(catalog())))],
                        base + [("Transfer-Encoding", "chunked")]):
            response = Response(catalog(), headers=headers)
            self.assertEqual(self.fetch_response(response)[0][0], 200)
            self.assertEqual(response.read_sizes, [guard.MAX_RESPONSE_BYTES + 1])

    def test_body_read_is_bounded_and_truncation_is_not_partial_success(self):
        for response in (Response(b" " * (guard.MAX_RESPONSE_BYTES + 1)),
                         Response(catalog(), headers=[("Content-Type", "application/json"),
                                                       ("Content-Length", str(len(catalog()) + 1))])):
            self.assert_fixed(self.fetch_response(response)[0], 502)
            self.assertEqual(response.read_sizes, [guard.MAX_RESPONSE_BYTES + 1])
        payload = catalog() + b" " * (guard.MAX_RESPONSE_BYTES - len(catalog()))
        self.assertEqual(self.fetch_response(Response(payload))[0][0], 200)

    def test_network_protocol_and_cleanup_errors_never_reflect(self):
        for operation in ("request", "getresponse", "close"):
            for failure in (TimeoutError("private"), OSError("private"),
                            http.client.HTTPException("private")):
                connection = mock.Mock()
                connection.getresponse.return_value = Response(catalog())
                getattr(connection, operation).side_effect = failure
                with mock.patch.object(guard.http.client, "HTTPConnection", return_value=connection):
                    self.assert_fixed(guard.catalog_response(AUTH, guard.fetch_catalog), 502)
                self.assertEqual(connection.close.call_count, 1)

    def test_invalid_authorization_cannot_open_transport(self):
        with mock.patch.object(guard.http.client, "HTTPConnection") as factory:
            with self.assertRaises(ValueError):
                guard.fetch_catalog("Basic invalid")
        self.assertEqual(factory.call_count, 0)


class HttpTests(OfflineCase):
    def test_valid_request_is_fixed_uncacheable_json_without_server_signature(self):
        self.assertEqual(self.request()[0], 200)
        self.fetch.assert_called_once_with(AUTH)

    def test_denials_and_failures_have_the_same_safe_http_headers(self):
        for response, expected in ((Response(catalog([])), 403),
                                   (Response(b"private", status=401), 403),
                                   (Response(b"private", status=403), 403),
                                   (Response(b"private", status=500), 502),
                                   (Response(b"private"), 502)):
            with mock.patch.object(guard.http.client, "HTTPConnection") as factory:
                factory.return_value.getresponse.return_value = response
                self.fetch = guard.fetch_catalog
                self.assert_fixed(self.request(), expected)

    def test_missing_malformed_duplicate_and_oversized_authorization(self):
        bad = [[], [("Authorization", "")], [("Authorization", "Basic invalid")],
               [("Authorization", "Bearer sk-bf-")], [("Authorization", "bearer sk-bf-x")],
               [("Authorization", AUTH + " ")], [("Authorization", AUTH + "," + AUTH)],
               [("Authorization", "Bearer  sk-bf-x")], [("Authorization", "Bearer sk-bf-é")],
               [("Authorization", AUTH), ("authorization", AUTH)],
               [("Authorization", "Bearer sk-bf-" + "a" * 4096)]]
        for index, headers in enumerate(bad):
            with self.subTest(case=index):
                self.assert_fixed(self.request(headers), 401)
        self.assertEqual(self.fetch.call_count, 0)
        exact = "Bearer sk-bf-" + "a" * (4096 - len("Bearer sk-bf-"))
        self.assertEqual(self.request([("Authorization", exact)])[0], 200)
        self.assert_fixed(self.request([("Authorization", exact + "a")]), 401)

    def test_alternate_credentials_headers_and_cookies_never_reach_upstream(self):
        alternates = [(name, "synthetic-not-a-real-credential") for name in (
            "Cookie", "Proxy-Authorization", "X-Bf-Vk", "X-Bf-Direct-Key", "X-Api-Key",
            "X-Goog-Api-Key", "Api-Key", "X-Bf-Provider", "Forwarded", "X-Forwarded-Host",
            "X-Forwarded-For", "X-Original-URL", "Accept", "Accept-Encoding", "User-Agent")]
        self.assert_fixed(self.request(alternates), 401)
        self.assert_fixed(self.request([("Authorization", "invalid"), *alternates]), 401)
        self.assertEqual(self.fetch.call_count, 0)
        with mock.patch.object(guard.http.client, "HTTPConnection") as factory:
            factory.return_value.getresponse.return_value = Response(catalog())
            self.fetch = guard.fetch_catalog
            self.assertEqual(self.request([("Authorization", AUTH), *alternates])[0], 200)
        headers = factory.return_value.request.call_args.kwargs["headers"]
        self.assertTrue(headers == {"Authorization": AUTH, "Accept": "application/json",
                                   "Accept-Encoding": "identity"}, "Only canonical headers may be sent")

    def test_only_exact_path_and_get_are_supported(self):
        for target in ("/", "/api/config", "/metrics", "/v1/models/", "//v1/models", "///v1/models",
                       "/v1/%6dodels", "/v1/models#fragment", "http://localhost/v1/models",
                       "/v1/other/../models", "/v1/models;parameter"):
            self.assert_fixed(self.request(target=target), 404)
        for target in ("/v1/models?", "/v1/models?provider=other", "/v1/models?unfiltered=true"):
            self.assert_fixed(self.request(target=target), 400)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "UNKNOWN"):
            self.assert_fixed(self.request(method=method), 404)
        code, body = self.request(method="HEAD")
        self.assertEqual((code, body), (404, b""))
        self.assertEqual(self.fetch.call_count, 0)

    def test_request_bodies_ambiguous_framing_and_protocol_errors_are_rejected(self):
        for headers in ([("Content-Length", "1")], [("Content-Length", "0")] * 2,
                        [("Content-Length", "-1")], [("Transfer-Encoding", "chunked")],
                        [("Expect", "100-continue")], [("Upgrade", "websocket")]):
            self.assert_fixed(self.request([("Authorization", AUTH), *headers]), 400)
        for raw in (b"GET /v1/models extra HTTP/1.1\r\n\r\n", b"GET /v1/models\r\n\r\n",
                    b"GET /v1/models HTTP/2.0\r\n\r\n",
                    b"GET /v1/models HTTP/1.1\r\nX-Large: " + b"a" * 65537 + b"\r\n\r\n",
                    b"GET /" + b"a" * 65537 + b" HTTP/1.1\r\n\r\n"):
            self.assert_fixed(self.request(raw=raw), 400)
        folded = ("GET /v1/models HTTP/1.1\r\nAuthorization: " + AUTH + "\r\n folded\r\n\r\n").encode()
        self.assert_fixed(self.request(raw=folded), 401)
        self.assertEqual(self.fetch.call_count, 0)
        self.assertEqual(self.request([("Authorization", AUTH), ("Content-Length", "0")])[0], 200)

    def test_pipeline_is_closed_after_one_request(self):
        raw = ("GET /v1/models HTTP/1.1\r\nAuthorization: " + AUTH + "\r\n\r\n").encode()
        self.assertEqual(self.request(raw=raw + raw)[0], 200)
        self.assertEqual(self.fetch.call_count, 1)

    def test_reflections_and_logging_hooks_are_silent_on_all_paths(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.request()
            self.assert_fixed(self.request(target="/synthetic-private-diagnostic"), 404)
            self.assert_fixed(self.request(method="synthetic-private-diagnostic"), 404)
            self.fetch.side_effect = ValueError("synthetic-private-diagnostic")
            self.assert_fixed(self.request(), 502)
            handler = object.__new__(guard.CatalogHandler)
            for log in (handler.log_request, handler.log_message, handler.log_error):
                log("synthetic-private-diagnostic")
            server = object.__new__(guard.CatalogServer)
            server.handle_error(None, ("127.0.0.1", 1))
        self.assertEqual(output.getvalue() + errors.getvalue(), "")


class ServerTests(OfflineCase):
    def server(self):
        # Bind suppression is local to this fixture; production cannot override
        # the listen address. Tests never touch even the real loopback services.
        with mock.patch.object(ThreadingHTTPServer, "__init__", return_value=None) as initialize:
            server = guard.CatalogServer(self.fetch)
        initialize.assert_called_once_with(("127.0.0.1", 9092), guard.CatalogHandler)
        return server

    def test_fixed_bind_backlog_daemon_threads_and_os_socket_timeout(self):
        server = self.server()
        self.assertIsInstance(server, ThreadingHTTPServer)
        self.assertTrue(server.daemon_threads)
        self.assertFalse(server.block_on_close)
        self.assertEqual(server.request_queue_size, 16)
        # An AF_UNIX socket pair exercises the real OS timeout without TCP.
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        with mock.patch.object(ThreadingHTTPServer, "get_request", return_value=(left, ("127.0.0.1", 1))):
            connection, _ = server.get_request()
        self.assertEqual(connection.gettimeout(), 5)

    def test_socket_timeout_failure_closes_socket(self):
        server = self.server()
        connection = mock.Mock()
        connection.settimeout.side_effect = OSError("synthetic-private-diagnostic")
        with mock.patch.object(ThreadingHTTPServer, "get_request", return_value=(connection, ("127.0.0.1", 1))):
            with self.assertRaises(OSError):
                server.get_request()
        connection.close.assert_called_once_with()

    def test_no_more_than_16_threads_and_no_waiting_overflow_thread(self):
        server = self.server()
        with (mock.patch.object(ThreadingHTTPServer, "process_request") as start,
              mock.patch.object(server, "shutdown_request") as close):
            for index in range(17):
                server.process_request(object(), ("127.0.0.1", index))
            self.assertEqual(start.call_count, 16)
            self.assertEqual(close.call_count, 1)
            with mock.patch.object(ThreadingHTTPServer, "process_request_thread"):
                server.process_request_thread(object(), ("127.0.0.1", 1))
            server.process_request(object(), ("127.0.0.1", 18))
            self.assertEqual(start.call_count, 17)

    def test_real_worker_completes_and_returns_slot(self):
        server = self.server()
        entered, release = threading.Event(), threading.Event()
        threads = []

        def finish(request, address):
            threads.append(threading.current_thread())
            entered.set()
            release.wait(5)

        with (mock.patch.object(server, "finish_request", side_effect=finish),
              mock.patch.object(server, "shutdown_request")):
            server.process_request(object(), ("127.0.0.1", 1))
            try:
                self.assertTrue(entered.wait(5), "Worker must start")
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        for _ in range(16):
            self.assertTrue(server._slots.acquire(blocking=False))
        self.assertFalse(server._slots.acquire(blocking=False))

    def test_thread_start_and_cleanup_failures_release_slots_silently(self):
        server = self.server()
        output, errors = io.StringIO(), io.StringIO()
        with (redirect_stdout(output), redirect_stderr(errors),
              mock.patch.object(ThreadingHTTPServer, "process_request", side_effect=RuntimeError("private")),
              mock.patch.object(server, "shutdown_request")):
            for index in range(17):
                server.process_request(object(), ("127.0.0.1", index))
        self.assertTrue(server._slots.acquire(blocking=False))
        with (redirect_stdout(output), redirect_stderr(errors),
              mock.patch.object(ThreadingHTTPServer, "process_request_thread", side_effect=OSError("private"))):
            server.process_request_thread(object(), ("127.0.0.1", 1))
        for _ in range(16):
            self.assertTrue(server._slots.acquire(blocking=False))
        self.assertFalse(server._slots.acquire(blocking=False))
        with (redirect_stdout(output), redirect_stderr(errors),
              mock.patch.object(ThreadingHTTPServer, "shutdown_request", side_effect=OSError("private"))):
            server.shutdown_request(object())
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_main_is_silent_uses_fixed_server_and_accepts_no_arguments(self):
        with mock.patch.object(guard, "CatalogServer") as factory:
            self.assertEqual(guard.main(), 0)
        factory.assert_called_once_with()
        factory.return_value.__enter__.return_value.serve_forever.assert_called_once_with()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            for failure, expected in ((OSError("private"), 1), (KeyboardInterrupt(), 0)):
                with mock.patch.object(guard, "CatalogServer", side_effect=failure):
                    self.assertEqual(guard.main(), expected)
            with mock.patch.object(guard.sys, "argv", [str(SCRIPT), "--unexpected"]):
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(SCRIPT), run_name="__main__")
            self.assertEqual(result.exception.code, 1)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()