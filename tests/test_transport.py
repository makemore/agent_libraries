"""Offline transport contract tests; HTTP(S) connections are always mocked."""

import http.client
import io
import json
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from agentctl import transport
from agentctl.errors import CLIError
from agentctl.transport import Client


class Response:
    def __init__(self, body=b'{"results":[]}', *, status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "application/json"} if headers is None else headers
        self.stream = io.BytesIO(body)
        self.read_sizes = []
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, count):
        self.read_sizes.append(count)
        return self.stream.read(count)

    def close(self):
        self.closed = True


class Connection:
    def __init__(self):
        self.response = Response()
        self.requests = []
        self.closed = False
        self.failure = None

    def request(self, method, target, *, headers):
        self.requests.append((method, target, headers))
        if self.failure is not None:
            raise self.failure

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    connection = Connection()
    https = Mock(return_value=connection)
    http = Mock(return_value=connection)
    # Do not read the real trust store in tests; assert default verified settings
    # on the injected context and that create_default_context takes no overrides.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls = Mock(return_value=context)
    monkeypatch.setattr(transport.http.client, "HTTPSConnection", https)
    monkeypatch.setattr(transport.http.client, "HTTPConnection", http)
    monkeypatch.setattr(transport.ssl, "create_default_context", tls)

    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    return SimpleNamespace(connection=connection, https=https, http=http, tls=tls, context=context)


@pytest.fixture
def client():
    return Client("https://api.example.test", "synthetic-active-token")


def assert_safe(error, code, exit_code, status=None):
    assert error.code == code
    assert error.exit_code == exit_code
    assert error.status == status
    assert str(error) in {
        "Invalid server response or request target.", "Authentication or permission denied.",
        "Resource not found.", "HTTP request failed.", "Request transport failed.",
        "Invalid request timeout.", "Invalid authentication scheme.",
        "Invalid or unavailable local configuration.", "A valid credential is required.",
    }
    assert "synthetic" not in json.dumps(error.payload())


def test_get_json_verified_tls_and_default_timeout(client, offline):
    assert client.get("/custom/agents/", {"page": 2, "system": "example value"}) == {"results": []}
    offline.https.assert_called_once_with("api.example.test", None, timeout=15, context=offline.context)
    offline.http.assert_not_called()
    offline.tls.assert_called_once_with()
    assert offline.context.check_hostname is True
    assert offline.context.verify_mode == ssl.CERT_REQUIRED
    assert offline.connection.closed
    assert len(offline.connection.requests) == 1
    method, target, headers = offline.connection.requests[0]
    assert method == "GET"
    assert target == "/custom/agents/?page=2&system=example+value"
    assert headers == {"Authorization": "Token synthetic-active-token", "Accept": "application/json",
                       "Accept-Encoding": "identity"}
    assert offline.connection.response.read_sizes == [transport._RESPONSE_LIMIT + 1]


def test_explicit_bearer_scheme_and_timeout(offline):
    client = Client("https://api.example.test:8443", "synthetic-active-token", scheme="Bearer", timeout=120)
    client.get("/identity")
    assert offline.https.call_args.args == ("api.example.test", 8443)
    assert offline.https.call_args.kwargs["timeout"] == 120
    assert offline.connection.requests[0][2]["Authorization"] == "Bearer synthetic-active-token"


def test_explicit_loopback_http_exception(offline):
    # Named development-only exception: no TLS bypass exists for remote hosts.
    client = Client("http://127.0.0.1:8123", "synthetic-active-token", allow_http_loopback=True)
    assert client.get("/agents/") == {"results": []}
    offline.http.assert_called_once_with("127.0.0.1", 8123, timeout=15)
    offline.https.assert_not_called()
    offline.tls.assert_not_called()
    assert client.next_query("http://127.0.0.1:8123/agents/?page=2", "/agents/") == {"page": "2"}


@pytest.mark.parametrize("timeout", [0, -1, 120.1, float("inf"), float("nan"), True, "15", None, 10**1000])
def test_invalid_timeouts_fail_before_connection(timeout, offline):
    with pytest.raises(CLIError) as caught:
        Client("https://api.example.test", "synthetic-active-token", timeout=timeout)
    assert_safe(caught.value, "configuration", 2)
    offline.https.assert_not_called()


@pytest.mark.parametrize("scheme", ["Basic", "token", "", None, "Token\r\nInjected: bad", []])
def test_invalid_authentication_schemes(scheme, offline):
    with pytest.raises(CLIError) as caught:
        Client("https://api.example.test", "synthetic-active-token", scheme=scheme)
    assert_safe(caught.value, "configuration", 2)
    offline.https.assert_not_called()


@pytest.mark.parametrize("token", ["", "with space", "line\r\nbreak", "\x00", "é", "x" * 8193, None],
                         ids=lambda value: "invalid-token")
def test_invalid_credentials_cannot_become_headers(token, offline):
    with pytest.raises(CLIError) as caught:
        Client("https://api.example.test", token)
    assert_safe(caught.value, "auth", 3)
    offline.https.assert_not_called()


@pytest.mark.parametrize("path", [
    "https://evil.test/", "//evil.test/", "relative", "/agents/?token=x", "/agents/#x",
    "/agents/../users/", "/agents/%2e%2e/", "/agents/\\evil", "/agents/\r\nInjected: x", None,
])
def test_unsafe_request_paths_fail_before_connection(client, offline, path):
    with pytest.raises(CLIError) as caught:
        client.get(path)
    assert_safe(caught.value, "protocol", 6)
    offline.https.assert_not_called()


@pytest.mark.parametrize("query", [
    {"token": "synthetic-forbidden-token"}, {"redirect": "https://evil.test/"}, {"other": 1},
    {"page": [1, 2]}, {"page": True}, {"page": None}, {"page": 1.5}, [("page", 1), ("page", 2)],
    {"cursor": "abc\n"}, {"cursor": "\x1b[31m"}, {"cursor": "\ud800"}, {"cursor": "x" * 8193},
])
def test_query_allowlist_types_and_controls(client, offline, query):
    with pytest.raises(CLIError) as caught:
        client.get("/agents/", query)
    assert_safe(caught.value, "protocol", 6)
    offline.https.assert_not_called()


def test_all_supported_query_keys(client, offline):
    query = {"page": 1, "page_size": 10, "limit": 20, "offset": 0,
             "cursor": "abc=", "system": "value", "agent_key": "agent"}
    client.get("/agents/", query)
    assert offline.connection.requests[0][1] == (
        "/agents/?page=1&page_size=10&limit=20&offset=0&cursor=abc%3D&system=value&agent_key=agent"
    )


def test_studio_filters_and_opaque_identifier(client, offline):
    client.get("/workspace/threads/", {"project_id": "host:7", "archived": "false"})
    assert offline.connection.requests[0][1] == (
        "/workspace/threads/?project_id=host%3A7&archived=false"
    )
    offline.connection.response = Response()
    client.get("/workspace/projects/host%3A7/")
    assert offline.connection.requests[1][1] == "/workspace/projects/host%3A7/"


@pytest.mark.parametrize(("status", "code", "exit_code"), [
    (401, "auth", 3), (403, "auth", 3), (404, "not_found", 4), (400, "http", 7),
    (429, "http", 7), (500, "http", 7), (503, "http", 7),
    (300, "http", 7), (301, "http", 7), (302, "http", 7), (303, "http", 7),
    (307, "http", 7), (308, "http", 7),
])
def test_http_errors_and_redirects_never_read_body_or_follow(client, offline, status, code, exit_code):
    response = Response(b"synthetic-private-server-body\x1b[31m", status=status, headers={
        "Location": "https://synthetic-user:synthetic-password@evil.test/?token=synthetic-active-token",
        "WWW-Authenticate": "synthetic-auth-header", "Set-Cookie": "synthetic-cookie",
    })
    offline.connection.response = response
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, code, exit_code, status)
    assert response.read_sizes == []
    assert len(offline.connection.requests) == 1
    assert offline.connection.closed
    assert response.closed


@pytest.mark.parametrize("failure", [
    OSError("synthetic-private-error"), TimeoutError("synthetic-private-error"),
    ssl.SSLCertVerificationError("synthetic-private-certificate"),
    http.client.BadStatusLine("synthetic-private-status"),
    http.client.IncompleteRead(b"synthetic-private-body"),
], ids=lambda value: type(value).__name__)
def test_transport_errors_are_static_and_close_connections(client, offline, failure):
    offline.connection.failure = failure
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, "transport", 5)
    assert caught.value.__suppress_context__
    assert offline.connection.closed


def test_connection_creation_failure_is_safe(client, offline):
    offline.https.side_effect = OSError("synthetic-private-error")
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, "transport", 5)


@pytest.mark.parametrize("body", [
    b"", b"<html>synthetic-private-html</html>", b"{invalid", b"\xff", b'{"key":"\xff"}',
    b"NaN", b"Infinity", b"-Infinity", b"1e999", b'{"a":1,"a":2}', b"{} {}",
    b"[" * 2000 + b"]" * 2000,
], ids=lambda value: "malformed-json")
def test_malformed_json_is_protocol_error(client, offline, body):
    offline.connection.response = Response(body)
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, "protocol", 6)
    assert offline.connection.closed


@pytest.mark.parametrize("headers", [
    {}, {"Content-Type": "text/html"}, {"Content-Type": "text/plain"},
    {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    {"Content-Type": "application/json", "Content-Length": "-1"},
    {"Content-Type": "application/json", "Content-Length": "2, 2"},
    {"Content-Type": "application/json", "Content-Length": "synthetic-private-header"},
    {"Content-Type": "application/json", "Content-Length": str(transport._RESPONSE_LIMIT + 1)},
])
def test_invalid_headers_or_declared_oversize_rejected_before_body(client, offline, headers):
    response = Response(headers=headers)
    offline.connection.response = response
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, "protocol", 6)
    assert response.read_sizes == []


@pytest.mark.parametrize("content_type", ["application/json; charset=utf-8", "APPLICATION/JSON",
                                               "application/problem+json"])
def test_json_media_types(client, offline, content_type):
    offline.connection.response = Response(b"[]", headers={"Content-Type": content_type, "Content-Length": "2"})
    assert client.get("/agents/") == []


def test_body_size_limit_without_length(client, offline):
    offline.connection.response = Response(b"x" * (transport._RESPONSE_LIMIT + 2))
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, "protocol", 6)
    assert offline.connection.response.stream.tell() == transport._RESPONSE_LIMIT + 1


def test_exact_body_limit_is_allowed(client, offline):
    content = b'"' + b"x" * (transport._RESPONSE_LIMIT - 2) + b'"'
    offline.connection.response = Response(content)
    assert len(client.get("/agents/")) == transport._RESPONSE_LIMIT - 2


def test_truncated_response_is_protocol_error(client, offline):
    offline.connection.response = Response(b"{}", headers={"Content-Type": "application/json", "Content-Length": "10"})
    with pytest.raises(CLIError) as caught:
        client.get("/agents/")
    assert_safe(caught.value, "protocol", 6)


def test_no_ambient_proxy_credentials_or_cookies(client, offline, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://synthetic-proxy.invalid:9999")
    monkeypatch.setenv("NETRC", "/nonexistent-synthetic-netrc")
    offline.connection.response = Response(headers={
        "Content-Type": "application/json", "Set-Cookie": "synthetic-cookie",
    })
    client.get("/agents/")
    offline.connection.response = Response()
    client.get("/agents/")
    for request in offline.connection.requests:
        headers = request[2]
        assert "Cookie" not in headers
        assert "Proxy-Authorization" not in headers
    assert offline.https.call_args.args[0] == "api.example.test"
    assert offline.https.call_count == 2


@pytest.mark.parametrize("url", [
    "https://api.example.test/agents/?page=2", "https://API.EXAMPLE.TEST:443/agents/?page=2",
    "/agents/?page=2", "?page=2",
])
def test_next_query_same_origin_same_resource(client, offline, url):
    assert client.next_query(url, "/agents/") == {"page": "2"}
    offline.https.assert_not_called()


def test_next_query_decodes_supported_filters(client):
    assert client.next_query("?cursor=abc%2B%2F%3D&system=two+words&agent_key=agent", "/agents/") == {
        "cursor": "abc+/=", "system": "two words", "agent_key": "agent",
    }


@pytest.mark.parametrize("url", [
    "https://evil.test/agents/?page=2", "http://api.example.test/agents/?page=2",
    "https://api.example.test:444/agents/?page=2", "//api.example.test/agents/?page=2",
    "https://synthetic-user:synthetic-password@api.example.test/agents/?page=2",
    "https://api.example.test/other/?page=2", "/agents/1/?page=2", "/agents?page=2",
    "/agents/../agents/?page=2", "/agents/%2e/?page=2", "/%61gents/?page=2",
    "?page=2#fragment", "?page=2#", "?page=2&page=3", "?page=2&%70age=3",
    "?token=synthetic-active-token", "?access_token=synthetic-active-token", "?password=x",
    "?redirect=https%3A%2F%2Fevil.test", "?next=/other/", "?page=2&unknown=x",
    "?page", "?page=2&", "?page=2&&limit=3", "?cursor=%", "?cursor=%zz", "?cursor=%ff",
    "?cursor=%0a", "?cursor=%00", "?cursor=%1b%5B31m", "?page=2\n", "?page=2 value",
    "?page=2\\evil", "?page=2&cursor=é", "agents/?page=2", "/agents/", "?", "", None, 1,
    "https://[::1]evil/agents/?page=2", "https:///agents/?page=2",
    "?" + "&".join(f"page={number}" for number in range(8)), "?cursor=" + "x" * 17000,
], ids=lambda value: "unsafe-pagination")
def test_next_query_rejects_target_escapes_credentials_and_ambiguity(client, offline, url):
    with pytest.raises(CLIError) as caught:
        client.next_query(url, "/agents/")
    assert_safe(caught.value, "protocol", 6)
    offline.https.assert_not_called()


def test_redaction_is_exact_and_repeated(client):
    assert client.redact("prefix synthetic-active-token suffix synthetic-active-token") == (
        "prefix [REDACTED] suffix [REDACTED]"
    )
    assert client.redact("synthetic-active-toke SYNTHETIC-ACTIVE-TOKEN") == (
        "synthetic-active-toke SYNTHETIC-ACTIVE-TOKEN"
    )
