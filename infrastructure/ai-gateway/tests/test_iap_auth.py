"""Isolated IAP verifier tests: ephemeral keys, memory HTTP, no cloud or secrets."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import runpy
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.error

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SPEC = importlib.util.spec_from_file_location("gateway_iap_auth", RUNTIME / "iap-auth.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
NOW = 1800000000
AUDIENCE = "/projects/123456789/global/backendServices/987654321"
EMAIL = "operator@example.invalid"


def config(**changes):
    return dict({"enabled": True, "audience": AUDIENCE, "allowed_emails": [EMAIL]}, **changes)


def claims(**changes):
    return dict({
        "sub": "synthetic-subject", "email": EMAIL, "iss": helper.ISSUER,
        "aud": AUDIENCE, "iat": NOW - 10, "exp": NOW + 590,
    }, **changes)


def public_pem(key):
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz=tz)


class Response(io.BytesIO):
    status = 200


class MemorySocket:
    """Run the real HTTP request parser without opening even a loopback port."""

    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()
        self.timeout = None

    def makefile(self, mode, buffering=None):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, timeout):
        self.timeout = timeout


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # Fail any accidentally unmocked outbound I/O before it reaches Google.
        for target in ("urllib.request.OpenerDirector.open", "urllib.request.urlopen"):
            patcher = mock.patch(target, side_effect=AssertionError("Network forbidden in isolated tests"))
            patcher.start()
            self.addCleanup(patcher.stop)


class SyntheticKeyCase(OfflineCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = ec.generate_private_key(ec.SECP256R1())
        cls.other_key = ec.generate_private_key(ec.SECP256R1())
        cls.pem = public_pem(cls.private_key)
        cls.other_pem = public_pem(cls.other_key)

    def setUp(self):
        super().setUp()
        patcher = mock.patch("jwt.api_jwt.datetime", FrozenDatetime)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(helper.time, "time", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = mock.Mock(return_value=1000.0)
        self.published = {"key-one": self.pem}
        self.opener = mock.Mock()
        self.opener.open.side_effect = lambda *args, **kwargs: Response(json.dumps(self.published).encode())
        self.cache = helper.KeyCache(self.opener, self.clock)
        self.verifier = helper.Verifier(config(), self.cache)

    def sign(self, payload=None, key=None, algorithm="ES256", headers=None):
        # PyJWT signs even deliberately malformed claim fixtures. No test
        # assertion/subTest includes the encoded assertion or private key.
        payload = claims() if payload is None else payload
        key = self.private_key if key is None and algorithm != "none" else key
        return jwt.api_jws.encode(
            json.dumps(payload, separators=(",", ":")).encode(), key,
            algorithm=algorithm, headers=dict({"kid": "key-one"}, **(headers or {})),
        )

    def assert_rejected(self, token):
        with self.assertRaises(helper.InvalidAssertion) as error:
            self.verifier.verify(token)
        self.assertTrue(str(error.exception) == "", "Authentication errors must be sanitized")

    def request(self, headers=(), method="GET", target="/verify", raw=None):
        if raw is None:
            lines = [f"{method} {target} HTTP/1.1", "Host: localhost", "Connection: close"]
            lines.extend(f"{name}: {value}" for name, value in headers)
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        connection = MemorySocket(raw)
        helper.AuthHandler(connection, ("127.0.0.1", 12345), SimpleNamespace(verifier=self.verifier))
        head, body = bytes(connection.output).split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        self.assertTrue(body == b"", "Verifier must not reflect content in a response body")
        self.assertEqual(connection.timeout, helper.SOCKET_TIMEOUT)
        return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:])


class ConfigTests(OfflineCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="iap-auth-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "iap-auth.json"

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def test_enabled_and_disabled_contract(self):
        self.assertEqual(helper.CONFIG_PATH, Path("/usr/local/lib/ai-gateway/iap-auth.json"))
        for value in (config(), config(enabled=False, audience=None, allowed_emails=[]), config(enabled=False)):
            self.write(value)
            self.assertEqual(helper.load_config(self.path), value)

    def test_configuration_rejects_wrong_types_missing_extra_and_unsafe_values(self):
        invalid = [None, [], "invalid", {}, config(extra=True)]
        for name in config():
            value = config()
            del value[name]
            invalid.append(value)
        changes = [
            {"enabled": value} for value in (None, 0, 1, "true", [])
        ] + [
            {"audience": value} for value in (
                None, "", [AUDIENCE], 123, AUDIENCE + "/", AUDIENCE + "\n",
                AUDIENCE + "?query", "/projects/name/global/backendServices/123",
                "/projects/123/apps/example", "/projects/１２３/global/backendServices/1",
            )
        ] + [
            {"allowed_emails": value} for value in (
                None, EMAIL, [], [EMAIL, EMAIL], [True], [None], [123], [[]],
                [""], ["no-domain"], ["user@@example.invalid"], [" " + EMAIL],
                [EMAIL + "\n"], ["ü@example.invalid"], ["x" * 255 + "@example.invalid"],
            )
        ]
        invalid.extend(config(**change) for change in changes)
        # Disabled does not mean malformed fields are tolerated.
        invalid.extend((config(enabled=False, audience="bad"), config(enabled=False, allowed_emails=[None])))
        for index, value in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ValueError):
                self.write(value)
                helper.load_config(self.path)

    def test_malformed_duplicate_nonfinite_and_oversized_json(self):
        data = [
            b"not-json", b"\xff", b'{"enabled":false,"enabled":true}',
            b'{"enabled":NaN}', b"x" * (helper.MAX_RESPONSE_BYTES + 1),
        ]
        for index, value in enumerate(data):
            self.path.write_bytes(value)
            with self.subTest(case=index), self.assertRaises(ValueError):
                helper.load_config(self.path)

    def test_main_disabled_exits_without_binding_or_fetching(self):
        self.write(config(enabled=False, audience=None, allowed_emails=[]))
        with mock.patch.object(helper, "AuthServer") as server, mock.patch.object(helper, "fetch_public_keys") as fetch:
            self.assertEqual(helper.main(self.path), 0)
        self.assertEqual(server.call_count, 0)
        self.assertEqual(fetch.call_count, 0)

    def test_main_enabled_serves_and_failures_are_silent(self):
        self.write(config())
        with mock.patch.object(helper, "AuthServer") as server:
            self.assertEqual(helper.main(self.path), 0)
            self.assertEqual(server.return_value.__enter__.return_value.serve_forever.call_count, 1)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            for failure in (OSError("synthetic-sensitive-error"), ValueError("synthetic-sensitive-error")):
                with mock.patch.object(helper, "load_config", side_effect=failure):
                    self.assertEqual(helper.main(self.path), 1)
            with mock.patch.object(helper, "AuthServer", side_effect=OSError("synthetic-sensitive-error")):
                self.assertEqual(helper.main(self.path), 1)
        self.assertTrue(out.getvalue() == "" and err.getvalue() == "", "Entry point must be silent")

    def test_cli_accepts_no_arguments(self):
        with mock.patch.object(helper.sys, "argv", ["iap-auth.py", "--unexpected"]):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(RUNTIME / "iap-auth.py"), run_name="__main__")
        self.assertEqual(result.exception.code, 1)


class AssertionTests(SyntheticKeyCase):
    def test_valid_token_and_finite_fractional_dates(self):
        for payload in (claims(), claims(iat=NOW - 10.5, exp=NOW + 100.5), claims(nbf=NOW - 1)):
            self.assertTrue(self.verifier.verify(self.sign(payload)) is None)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_forged_signature_is_not_refreshed(self):
        self.verifier.verify(self.sign())
        self.clock.return_value += helper.REFRESH_INTERVAL
        self.assert_rejected(self.sign(key=self.other_key))
        self.assertEqual(self.opener.open.call_count, 1)

    def test_hs256_none_and_wrong_algorithm_rejected_before_key_fetch(self):
        tokens = (
            self.sign(key=b"synthetic-hmac-only-test-key-32-bytes", algorithm="HS256"),
            self.sign(algorithm="none"),
            self.sign(key=ec.generate_private_key(ec.SECP384R1()), algorithm="ES384"),
        )
        for index, token in enumerate(tokens):
            with self.subTest(case=index):
                self.assert_rejected(token)
        self.assertEqual(self.opener.open.call_count, 0)

    def test_exact_issuer_and_string_audience(self):
        changes = [
            {"iss": value} for value in ("https://invalid.example", "https://cloud.google.com", helper.ISSUER + "/", None, [helper.ISSUER])
        ] + [
            {"aud": value} for value in ("other", [AUDIENCE], [AUDIENCE, "other"], [], None, 123, AUDIENCE + "/")
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                self.assert_rejected(self.sign(claims(**change)))

    def test_required_claims_and_nonempty_subject(self):
        for name in helper.REQUIRED_CLAIMS:
            for null in (False, True):
                payload = claims()
                if null:
                    payload[name] = None
                else:
                    del payload[name]
                with self.subTest(claim=name, null=null):
                    self.assert_rejected(self.sign(payload))
        for index, subject in enumerate(("", " \t", False, 123, [])):
            with self.subTest(case=index):
                self.assert_rejected(self.sign(claims(sub=subject)))

    def test_expiration_future_issued_at_and_not_before_with_exact_leeway(self):
        valid = [claims(exp=NOW - 29, iat=NOW - 629), claims(iat=NOW + 30), claims(nbf=NOW + 30)]
        invalid = [
            claims(exp=NOW - 30, iat=NOW - 630), claims(exp=NOW - 31, iat=NOW - 631),
            claims(iat=NOW + 31), claims(iat=NOW + 30.5),
            claims(nbf=NOW + 31), claims(nbf=NOW + 30.5),
        ]
        for payload in valid:
            self.assertTrue(self.verifier.verify(self.sign(payload)) is None)
        for index, payload in enumerate(invalid):
            with self.subTest(case=index):
                self.assert_rejected(self.sign(payload))

    def test_lifetime_boundary_and_invalid_numeric_dates(self):
        self.assertTrue(self.verifier.verify(self.sign(claims(iat=NOW, exp=NOW + 660))) is None)
        for index, payload in enumerate((
            claims(iat=NOW, exp=NOW + 661), claims(iat=NOW, exp=NOW),
            claims(iat=NOW, exp=NOW - 1), claims(iat=NOW - 1000, exp=NOW + 1),
        )):
            with self.subTest(case=index):
                self.assert_rejected(self.sign(payload))
        for name in ("exp", "iat", "nbf"):
            for index, value in enumerate((True, False, float("nan"), float("inf"), -float("inf"), "1800000000", None, [], {}, 10 ** 400)):
                with self.subTest(claim=name, case=index):
                    self.assert_rejected(self.sign(claims(**{name: value})))

    def test_email_allowlist_is_exact_and_disabled_never_authenticates(self):
        for index, email in enumerate(("other@example.invalid", EMAIL.upper(), EMAIL + " ", "", None, [EMAIL], 123)):
            with self.subTest(case=index):
                self.assert_rejected(self.sign(claims(email=email)))
        self.verifier = helper.Verifier(config(enabled=False), self.cache)
        self.assert_rejected(self.sign())
        self.assertEqual(self.opener.open.call_count, 0)

    def test_malformed_tokens_headers_and_duplicate_claims(self):
        token = self.sign()
        malformed = (None, b"not-text", "", "not-a-jwt", "a.b.c", token + " ", " " + token,
                     token + ".extra", token.replace(".", ",", 1), "é", "a" * (helper.MAX_TOKEN_BYTES + 1))
        for index, value in enumerate(malformed):
            with self.subTest(case=index):
                self.assert_rejected(value)
        original = token.split(".")
        bad_headers = [b"[]", b"null", b"not-json", b'{"alg":"ES256","alg":"ES256","kid":"key-one"}']
        for kid in (None, "", [], 12, "../other", "x" * 257):
            bad_headers.append(json.dumps({"alg": "ES256", "kid": kid}).encode())
        bad_headers.extend((b'{"alg":"ES256"}', b'{"kid":"key-one"}'))
        for index, raw in enumerate(bad_headers):
            parts = original.copy()
            parts[0] = helper.base64url_encode(raw).decode()
            with self.subTest(header=index):
                self.assert_rejected(".".join(parts))
        for index, payload in enumerate((b"[]", b"null", b"\xff", b"{", json.dumps(claims()).encode()[:-1] + b',"exp":1800000100}')):
            signed = jwt.api_jws.encode(payload, self.private_key, algorithm="ES256", headers={"kid": "key-one"})
            with self.subTest(payload=index):
                self.assert_rejected(signed)
        self.assert_rejected(self.sign(headers={"crit": ["unknown"]}))
        self.assertEqual(self.opener.open.call_count, 0)

    def test_token_size_limit_including_exact_boundary(self):
        payload = claims(padding="")
        token = self.sign(payload)
        # JWT base64 lengths advance by 1 or 2; search only the boundary region.
        padding = ((helper.MAX_TOKEN_BYTES - len(token)) * 3) // 4
        candidates = [self.sign(claims(padding="x" * size)) for size in range(padding - 3, padding + 4)]
        exact = next(value for value in candidates if len(value) == helper.MAX_TOKEN_BYTES)
        self.assertTrue(self.verifier.verify(exact) is None)
        self.assert_rejected(next(value for value in candidates if len(value) > helper.MAX_TOKEN_BYTES))

    def test_token_urls_and_embedded_keys_never_select_a_key_or_destination(self):
        headers = {"jku": "https://attacker.invalid/keys", "x5u": "http://127.0.0.1/keys", "jwk": {"kty": "EC"}}
        self.assertTrue(self.verifier.verify(self.sign(headers=headers)) is None)
        self.assert_rejected(self.sign(key=self.other_key, headers=headers))
        self.clock.return_value += helper.REFRESH_INTERVAL
        self.assert_rejected(self.sign(headers=dict(headers, kid="unknown-key")))
        self.assertEqual(self.opener.open.call_count, 2)
        for call in self.opener.open.call_args_list:
            self.assertTrue(call.args[0].full_url == helper.KEY_URL, "Only the fixed Google URL may be fetched")
            self.assertTrue(call.args[0].header_items() == [], "Key fetch must not forward credentials")


class KeyNetworkTests(SyntheticKeyCase):
    def test_fixed_url_timeout_and_default_opener_security(self):
        with mock.patch.object(helper.urllib.request, "build_opener", return_value=self.opener) as build:
            self.assertEqual(helper.fetch_public_keys(), self.published)
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, helper.NoRedirect)
        self.assertIsNone(redirect.redirect_request(None, None, 302, None, None, "https://attacker.invalid"))
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.gstatic.com/iap/verify/public_key")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 5})
        self.assertIsNone(request.data)

    def test_bounded_response_and_strict_key_mapping(self):
        wrong_curve = public_pem(ec.generate_private_key(ec.SECP384R1()))
        private_pem = self.private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ).decode()
        values = [None, [], {}, {"": self.pem}, {"../key": self.pem}, {"key-one": None},
                  {"key-one": "not-pem"}, {"key-one": self.pem + "x" * 4096},
                  {"key-one": wrong_curve}, {"key-one": private_pem}]
        bodies = [json.dumps(value).encode() for value in values]
        bodies.extend((b"\xff", b"not-json", b'{"key-one":NaN}',
                       b'{"key-one":"a","key-one":"b"}', b"x" * (helper.MAX_RESPONSE_BYTES + 1)))
        for index, body in enumerate(bodies):
            self.opener.open.side_effect = lambda *args, **kwargs: Response(body)
            with self.subTest(case=index), self.assertRaises(ValueError):
                helper.fetch_public_keys(self.opener)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b"x" * (helper.MAX_RESPONSE_BYTES + 1)
        self.opener.open.side_effect = None
        self.opener.open.return_value = response
        with self.assertRaises(ValueError):
            helper.fetch_public_keys(self.opener)
        response.read.assert_called_once_with(helper.MAX_RESPONSE_BYTES + 1)

    def test_cache_hit_expiry_and_rotation(self):
        self.verifier.verify(self.sign())
        self.clock.return_value += helper.KEY_TTL - 1
        self.verifier.verify(self.sign())
        self.assertEqual(self.opener.open.call_count, 1)
        self.published = {"key-two": self.other_pem}
        self.clock.return_value += 1
        self.verifier.verify(self.sign(key=self.other_key, headers={"kid": "key-two"}))
        self.assertEqual(self.opener.open.call_count, 2)
        self.assert_rejected(self.sign())
        self.assertEqual(self.opener.open.call_count, 2)

    def test_unknown_kid_refresh_is_throttled_and_recovers_rotation(self):
        self.verifier.verify(self.sign())
        self.published["key-two"] = self.other_pem
        rotated = self.sign(key=self.other_key, headers={"kid": "key-two"})
        for offset in (0, helper.REFRESH_INTERVAL - 0.01):
            self.clock.return_value = 1000 + offset
            self.assert_rejected(rotated)
        self.assertEqual(self.opener.open.call_count, 1)
        self.clock.return_value = 1000 + helper.REFRESH_INTERVAL
        self.verifier.verify(rotated)
        self.assertEqual(self.opener.open.call_count, 2)
        for index in range(10):
            self.assert_rejected(self.sign(headers={"kid": f"missing-{index}"}))
        self.assertEqual(self.opener.open.call_count, 2)

    def test_network_and_bad_key_response_fail_closed_and_throttle_retries(self):
        for index, failure in enumerate((
            urllib.error.URLError("synthetic-sensitive-error"), TimeoutError("synthetic-sensitive-error"),
            urllib.error.HTTPError(helper.KEY_URL, 302, "synthetic-sensitive-error", {}, None),
        )):
            cache = helper.KeyCache(self.opener, self.clock)
            self.opener.open.reset_mock()
            self.opener.open.side_effect = failure
            for attempt in range(2):
                with self.subTest(case=index, attempt=attempt), self.assertRaises(helper.KeysUnavailable) as error:
                    cache.get("key-one")
                self.assertTrue(str(error.exception) == "", "Key errors must be sanitized")
            self.assertEqual(self.opener.open.call_count, 1)
        self.opener.open.side_effect = lambda *args, **kwargs: Response(b"{}")
        with self.assertRaises(helper.KeysUnavailable):
            helper.KeyCache(self.opener, self.clock).get("key-one")

    def test_expired_keys_are_never_used_after_refresh_failure(self):
        self.verifier.verify(self.sign())
        self.clock.return_value += helper.KEY_TTL
        self.opener.open.side_effect = urllib.error.URLError("synthetic-sensitive-error")
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaises(helper.KeysUnavailable):
                self.verifier.verify(self.sign())
        self.assertEqual(self.opener.open.call_count, 2)
        self.clock.return_value += helper.REFRESH_INTERVAL
        self.opener.open.side_effect = lambda *args, **kwargs: Response(json.dumps(self.published).encode())
        self.verifier.verify(self.sign())
        self.assertEqual(self.opener.open.call_count, 3)

    def test_unknown_kid_network_failure_does_not_extend_existing_key_ttl(self):
        self.verifier.verify(self.sign())
        self.clock.return_value += helper.REFRESH_INTERVAL
        self.opener.open.side_effect = OSError("synthetic-sensitive-error")
        with self.assertRaises(helper.KeysUnavailable):
            self.verifier.verify(self.sign(headers={"kid": "unknown"}))
        self.verifier.verify(self.sign())  # The original key is still unexpired.
        self.clock.return_value = 1000 + helper.KEY_TTL
        with self.assertRaises(helper.KeysUnavailable):
            self.verifier.verify(self.sign())

    def test_concurrent_cache_misses_share_one_refresh(self):
        barrier = threading.Barrier(8)

        def lookup(index):
            barrier.wait(timeout=5)
            return self.cache.get("key-one") == self.pem

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertTrue(all(executor.map(lookup, range(8))))
        self.assertEqual(self.opener.open.call_count, 1)


class HttpTests(SyntheticKeyCase):
    def test_valid_assertion_returns_empty_uncacheable_200(self):
        status, headers = self.request([(helper.ASSERTION_HEADER, self.sign())])
        self.assertEqual(status, 200)
        self.assertTrue(
            headers == {"Content-Length": "0", "Cache-Control": "no-store", "Connection": "close"},
            "Verifier responses must not disclose identity or request headers",
        )

    def test_no_jwt_invalid_forged_and_duplicate_headers_are_401(self):
        token = self.sign()
        cases = [
            [], [("Authorization", "Bearer synthetic-not-an-iap-assertion")],
            [(helper.ASSERTION_HEADER, "")], [(helper.ASSERTION_HEADER, "not-a-jwt")],
            [(helper.ASSERTION_HEADER, self.sign(key=self.other_key))],
            [(helper.ASSERTION_HEADER, token), (helper.ASSERTION_HEADER.lower(), token)],
            [(helper.ASSERTION_HEADER, token + "," + token)],
            [(helper.ASSERTION_HEADER, "x" * (helper.MAX_TOKEN_BYTES + 1))],
        ]
        for index, headers in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(self.request(headers)[0], 401)

    def test_key_network_failure_is_503_not_200_or_401(self):
        self.opener.open.side_effect = urllib.error.URLError("synthetic-sensitive-error")
        self.assertEqual(self.request([(helper.ASSERTION_HEADER, self.sign())])[0], 503)
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.request([(helper.ASSERTION_HEADER, "not-a-jwt")])[0], 401)

    def test_only_exact_get_verify_is_supported(self):
        headers = [(helper.ASSERTION_HEADER, self.sign())]
        for method in ("HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "UNKNOWN"):
            with self.subTest(method=method):
                self.assertEqual(self.request(headers, method=method)[0], 405)
        for index, target in enumerate(("/", "/verify/", "/verify?query", "/verify#fragment", "//verify", "/%76erify", "http://localhost/verify")):
            with self.subTest(case=index):
                self.assertEqual(self.request(headers, target=target)[0], 404)
        self.assertEqual(self.opener.open.call_count, 0)

    def test_requests_errors_and_explicit_logging_hooks_are_silent(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            token = self.sign()
            self.request([(helper.ASSERTION_HEADER, token)])
            self.request([(helper.ASSERTION_HEADER, token)], target="/synthetic-sensitive-uri")
            self.request([(helper.ASSERTION_HEADER, "synthetic-invalid")])
            self.request(method="UNKNOWN")
            self.assertEqual(self.request(raw=b"GET /verify extra HTTP/1.1\r\n\r\n")[0], 400)
            handler = object.__new__(helper.AuthHandler)
            handler.log_message("%s %s %s", token, EMAIL, "/synthetic-sensitive-uri")
            handler.log_error("%s", token)
            server = object.__new__(helper.AuthServer)
            server.handle_error(None, ("127.0.0.1", 1))
            self.clock.return_value += helper.KEY_TTL
            self.opener.open.side_effect = ValueError("synthetic-sensitive-error")
            self.assertEqual(self.request([(helper.ASSERTION_HEADER, token)])[0], 503)
        self.assertTrue(out.getvalue() == "" and err.getvalue() == "", "No request or error details may be logged")


class ServerTests(OfflineCase):
    def server(self):
        # This test-only bind suppression avoids touching the fixed service port;
        # the production constructor has no host/port override to misuse.
        with mock.patch.object(helper.HTTPServer, "__init__", return_value=None) as initialize:
            server = helper.AuthServer(mock.Mock())
        initialize.assert_called_once_with(("127.0.0.1", 9091), helper.AuthHandler)
        return server

    def test_fixed_bind_daemon_threads_and_socket_timeout(self):
        server = self.server()
        self.assertTrue(server.daemon_threads)
        self.assertFalse(server.block_on_close)
        connection = mock.Mock()
        with mock.patch.object(helper.HTTPServer, "get_request", return_value=(connection, ("127.0.0.1", 1))):
            server.get_request()
        connection.settimeout.assert_called_once_with(5)

    def test_concurrency_is_bounded_and_worker_releases_slot(self):
        server = self.server()
        with (
            mock.patch.object(helper.ThreadingMixIn, "process_request") as start,
            mock.patch.object(server, "shutdown_request") as close,
        ):
            for index in range(helper.MAX_WORKERS + 1):
                server.process_request(object(), ("127.0.0.1", index))
            self.assertEqual(start.call_count, helper.MAX_WORKERS)
            self.assertEqual(close.call_count, 1)
            with mock.patch.object(helper.ThreadingMixIn, "process_request_thread"):
                server.process_request_thread(object(), ("127.0.0.1", 1))
            server.process_request(object(), ("127.0.0.1", 1))
            self.assertEqual(start.call_count, helper.MAX_WORKERS + 1)

    def test_thread_start_failure_releases_slot_without_logging(self):
        server = self.server()
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(helper.ThreadingMixIn, "process_request", side_effect=RuntimeError("synthetic-sensitive-error")),
            mock.patch.object(server, "shutdown_request") as close,
            redirect_stdout(out), redirect_stderr(err),
        ):
            for index in range(helper.MAX_WORKERS + 1):
                server.process_request(object(), ("127.0.0.1", index))
        self.assertEqual(close.call_count, helper.MAX_WORKERS + 1)
        for index in range(helper.MAX_WORKERS):
            self.assertTrue(server._slots.acquire(blocking=False))
        self.assertFalse(server._slots.acquire(blocking=False))
        self.assertTrue(out.getvalue() == "" and err.getvalue() == "", "Worker failures must be silent")

    def test_worker_cleanup_failure_is_silent_and_releases_slot(self):
        server = self.server()
        self.assertTrue(server._slots.acquire(blocking=False))
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(helper.ThreadingMixIn, "process_request_thread", side_effect=OSError("synthetic-sensitive-error")),
            redirect_stdout(out), redirect_stderr(err),
        ):
            server.process_request_thread(object(), ("127.0.0.1", 1))
        for index in range(helper.MAX_WORKERS):
            self.assertTrue(server._slots.acquire(blocking=False))
        self.assertFalse(server._slots.acquire(blocking=False))
        self.assertTrue(out.getvalue() == "" and err.getvalue() == "", "Worker cleanup must be silent")


if __name__ == "__main__":
    unittest.main()