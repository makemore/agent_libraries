"""Synthetic private-admin checks; no live networking or operator credentials.

Run with root .venv/bin/python -B -m unittest discover
  -s infrastructure/business-tools/tests -p test_private_admin.py -v
"""

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import http.cookiejar
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
import traceback
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("business_tools_private_admin", ROOT / "scripts/private_admin.py")
admin = importlib.util.module_from_spec(SPEC)
# Register while executing so dataclass annotations can resolve the module.
with mock.patch.dict(sys.modules, {SPEC.name: admin}):
    SPEC.loader.exec_module(admin)

CREDENTIALS = {"email": "operator@example.invalid", "password": "synthetic-not-a-password",
               "username": "fixture-admin", "company": "Synthetic Company"}
TOKEN = "synthetic-not-a-token"


def response(payload=None, status=200, location=None):
    headers = Message()
    if location is not None:
        headers["Location"] = location
    return admin.Response(status, headers, json.dumps(payload).encode())


def client_with(*responses):
    client = mock.Mock()
    client.request.side_effect = responses
    client.cookies = http.cookiejar.CookieJar()
    return client


def auth_cookie(secure=True, httponly=True):
    return http.cookiejar.Cookie(
        version=0, name="auth", value=TOKEN, port=None, port_specified=False,
        domain=admin.HOSTS["postiz"], domain_specified=False, domain_initial_dot=False,
        path="/", path_specified=True, secure=secure, expires=None, discard=True,
        comment=None, comment_url=None, rest={"HttpOnly": None} if httponly else {},
    )


class OfflineTests(unittest.TestCase):
    def setUp(self):
        # Unexpected network use fails even if main() catches the exception.
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.getaddrinfo"):
            guard = self.enterContext(mock.patch(target, side_effect=AssertionError("No networking")))
            self.addCleanup(guard.assert_not_called)

    def cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = admin.main(argv)
        self.assertEqual(stderr.getvalue(), "")
        return result, stdout.getvalue()


class PreviewTests(OfflineTests):
    def test_default_previews_only_read_and_never_load_supplied_credentials(self):
        cases = (
            ("postiz", [response({"register": True})], [mock.call("GET", "/api/auth/can-register")]),
            ("postiz", [response({"register": False})], [mock.call("GET", "/api/auth/can-register")]),
            ("actual", [response({"data": {"bootstrapped": False}})],
             [mock.call("GET", "/account/needs-bootstrap")]),
            ("actual", [response({"data": {"bootstrapped": True}})],
             [mock.call("GET", "/account/needs-bootstrap")]),
            ("grafana", [response(status=401)], [mock.call("GET", "/api/user")]),
            ("invoice_ninja", [response(status=401), response(status=404)],
             [mock.call("GET", "/api/v1/clients"), mock.call("GET", "/setup")]),
        )
        for app, replies, calls in cases:
            for extra in ([], ["--credentials", "/synthetic/never-open.json"]):
                with self.subTest(app=app, supplied_path=bool(extra)):
                    client = client_with(*replies)
                    with mock.patch.object(admin, "Client", return_value=client) as factory, \
                            mock.patch.object(admin, "protected_json") as loader:
                        code, output = self.cli(["--app", app, *extra])
                    self.assertEqual(code, 0)
                    self.assertTrue(json.loads(output)["preview"])
                    loader.assert_not_called()
                    factory.assert_called_once_with(admin.HOSTS[app], 18443)
                    self.assertEqual(client.request.call_args_list, calls)

    def test_mautic_cli_delegates_preview_without_credentials_or_install_proof(self):
        initializer = mock.Mock(return_value={"preview": True})
        with mock.patch.dict(sys.modules, {"mautic_admin": mock.Mock(initialize=initializer)}), \
                mock.patch.object(admin, "Client") as factory, \
                mock.patch.object(admin, "protected_json") as loader:
            code, output = self.cli(["--app", "mautic", "--credentials", "/synthetic/never-open.json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["preview"])
        initializer.assert_called_once_with(factory.return_value, None, None)
        loader.assert_not_called()

    def test_apply_requires_credentials_and_mautic_apply_is_not_exposed(self):
        cases = [["--app", app, "--apply"] for app in admin.HOSTS]
        cases.append(["--app", "mautic", "--apply", "--credentials", "/synthetic/never-open.json"])
        for argv in cases:
            with self.subTest(app=argv[1]), mock.patch.object(admin, "Client") as factory, \
                    mock.patch.object(admin, "protected_json") as loader:
                self.assertEqual(self.cli(argv), (1, admin.ERROR + "\n"))
                loader.assert_not_called()
                factory.assert_not_called()


class AccountTests(OfflineTests):
    def postiz_login(self):
        return [response({"login": True}), response({"email": CREDENTIALS["email"], "role": "ADMIN"}),
                response({"register": False})]

    def test_postiz_registers_once_and_existing_account_only_logs_in(self):
        body = {key: CREDENTIALS[key] for key in ("email", "password")}
        body["provider"] = "LOCAL"
        for first_run in (True, False):
            replies = [response({"register": first_run})]
            calls = [mock.call("GET", "/api/auth/can-register")]
            if first_run:
                replies.append(response({"register": True, "activate": False}))
                calls.append(mock.call("POST", "/api/auth/register", data={**body, "company": CREDENTIALS["company"]}))
            client = client_with(*replies, *self.postiz_login())
            client.cookies.set_cookie(auth_cookie())
            with self.subTest(first_run=first_run):
                result = admin.postiz(client, CREDENTIALS, apply=True)
                self.assertTrue(result["login_verified"])
                self.assertTrue(result["registration_closed"])
                self.assertTrue(result["secure_httponly_cookie"])
                self.assertEqual(client.request.call_args_list, calls + [
                    mock.call("POST", "/api/auth/login", data=body),
                    mock.call("GET", "/api/user/self"), mock.call("GET", "/api/auth/can-register")])

    def test_postiz_requires_secure_httponly_auth_cookie(self):
        for cookies in ([], [auth_cookie(secure=False)], [auth_cookie(httponly=False)]):
            with self.subTest(cookie_count=len(cookies)):
                client = client_with(response({"register": False}), *self.postiz_login())
                for cookie in cookies:
                    client.cookies.set_cookie(cookie)
                with self.assertRaisesRegex(RuntimeError, admin.ERROR):
                    admin.postiz(client, CREDENTIALS, apply=True)

    def test_actual_creates_once_and_reruns_preserve_password(self):
        # Actual deliberately probes bootstrap rejection after login. Count
        # successful creations separately from these explicitly denied POSTs.
        stored = {"password": None, "creations": 0, "denials": 0}

        def request(method, path, data=None, headers=None):
            if (method, path) == ("GET", "/account/needs-bootstrap"):
                return response({"data": {"bootstrapped": stored["password"] is not None}})
            if (method, path) == ("POST", "/account/bootstrap"):
                if stored["password"] is not None:
                    stored["denials"] += 1
                    return response({"reason": "already-bootstrapped"}, status=400)
                stored["password"] = data["password"]
                stored["creations"] += 1
                return response({"status": "ok"})
            if (method, path) == ("POST", "/account/login"):
                self.assertEqual(data, {"password": stored["password"]})
                return response({"status": "ok", "data": {"token": TOKEN}})
            if (method, path) == ("GET", "/account/validate"):
                self.assertEqual(headers, {"X-Actual-Token": TOKEN})
                return response({"data": {"validated": True, "permission": "ADMIN", "loginMethod": "password"}})
            if (method, path) == ("GET", "/sync/list-user-files"):
                self.assertIsNone(headers)
                return response({"status": "error"}, status=401)
            self.fail("Unexpected synthetic request")

        client = client_with()
        client.request.side_effect = request
        for run in range(2):
            self.assertTrue(admin.actual(client, CREDENTIALS, apply=True)["repeat_bootstrap_denied"])
            self.assertEqual(stored, {"password": CREDENTIALS["password"], "creations": 1, "denials": run + 1})

    def test_existing_bad_credentials_fail_without_creation_or_reset(self):
        cases = (
            ("postiz", [response({"register": False}), response(status=401)],
             [("GET", "/api/auth/can-register"), ("POST", "/api/auth/login")]),
            ("actual", [response({"data": {"bootstrapped": True}}), response(status=401)],
             [("GET", "/account/needs-bootstrap"), ("POST", "/account/login")]),
            ("grafana", [response(status=401), response(status=401)],
             [("GET", "/api/user"), ("GET", "/api/user")]),
            ("invoice_ninja", [response(status=401), response(status=404), response(status=401)],
             [("GET", "/api/v1/clients"), ("GET", "/setup"), ("POST", "/api/v1/login")]),
        )
        for app, replies, calls in cases:
            with self.subTest(app=app):
                client = client_with(*replies)
                with self.assertRaisesRegex(RuntimeError, admin.ERROR):
                    getattr(admin, app)(client, CREDENTIALS, apply=True)
                self.assertEqual([call.args for call in client.request.call_args_list], calls)

    def test_grafana_updates_only_bootstrap_email_and_rerun_is_read_only(self):
        for existing in ("", "admin@localhost", CREDENTIALS["email"]):
            with self.subTest(existing=existing):
                user = {"login": CREDENTIALS["username"], "isGrafanaAdmin": True,
                        "email": existing, "name": "Preserved Operator Name"}
                replies = [response(status=401), response(user)]
                if existing != CREDENTIALS["email"]:
                    replies.append(response({"message": "updated"}))
                replies.extend([
                    response({**user, "email": CREDENTIALS["email"]}),
                    response({"auth.anonymous": {"enabled": "false"}, "users": {"allow_sign_up": "false"},
                              "security": {"cookie_secure": "true"}}),
                    response({"status": "OK"}),
                ])
                client = client_with(*replies)
                self.assertTrue(admin.grafana(client, CREDENTIALS, apply=True)["email_matches"])
                writes = [call for call in client.request.call_args_list if call.args[0] != "GET"]
                if existing == CREDENTIALS["email"]:
                    self.assertEqual(writes, [])
                else:
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0].args, ("PUT", "/api/user"))
                    self.assertEqual(writes[0].kwargs["data"], {
                        "email": CREDENTIALS["email"], "name": user["name"], "login": user["login"]})

    def test_grafana_never_overwrites_nonbootstrap_or_unknown_email(self):
        for existing in ("owner@example.invalid", "ADMIN@localhost", None):
            with self.subTest(existing=existing):
                client = client_with(response(status=401), response({
                    "login": CREDENTIALS["username"], "isGrafanaAdmin": True, "email": existing}))
                with self.assertRaisesRegex(RuntimeError, admin.ERROR):
                    admin.grafana(client, CREDENTIALS, apply=True)
                self.assertEqual([call.args for call in client.request.call_args_list],
                                 [("GET", "/api/user"), ("GET", "/api/user")])

    def test_invoice_existing_owner_login_never_creates_an_account(self):
        client = client_with(response(status=403), response(status=404), response({"data": [{
            "is_owner": True, "is_admin": True, "user": {"email": CREDENTIALS["email"]}}]}))
        self.assertTrue(admin.invoice_ninja(client, CREDENTIALS, apply=True)["owner_and_administrator"])
        self.assertEqual(client.request.call_args_list, [mock.call("GET", "/api/v1/clients"),
                         mock.call("GET", "/setup"), mock.call("POST", "/api/v1/login", data={
                             "email": CREDENTIALS["email"], "password": CREDENTIALS["password"]})])

    def test_invoice_setup_redirect_must_be_canonical_root(self):
        origin = 'https://' + admin.HOSTS['invoice_ninja']
        for location in (origin, origin + '/', origin + '/setup', '/login',
                         'http://' + admin.HOSTS['invoice_ninja'], origin + '?token=synthetic'):
            client = client_with(response(status=401), response(status=302, location=location))
            client.host = admin.HOSTS['invoice_ninja']
            if location in (origin, origin + '/'):
                self.assertTrue(admin.invoice_ninja(client)['setup_unavailable'])
            else:
                with self.assertRaises(RuntimeError):
                    admin.invoice_ninja(client)


class PostizRegistrationDenialTests(OfflineTests):
    def closed_client(self):
        return client_with(response({"register": False}),
                           admin.Response(400, Message(), b"Registration is disabled"),
                           response({"register": False}))

    def assert_failure(self, client):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(RuntimeError) as caught:
                admin.postiz_registration_denial(client)
        self.assertEqual(str(caught.exception), admin.ERROR)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_closed_policy_exact_denial_and_recheck_return_only_safe_summary(self):
        client = self.closed_client()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = admin.postiz_registration_denial(client)
        self.assertEqual(result, {"api_second_signup_denied": True})
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual([call.args for call in client.request.call_args_list], [
            ("GET", "/api/auth/can-register"), ("POST", "/api/auth/register"),
            ("GET", "/api/auth/can-register")])
        self.assertEqual(len(client.cookies), 0)

    def test_policy_must_be_strictly_false_before_generating_or_posting(self):
        replies = [response({"register": value}) for value in (True, "false", "False", None, 0, [], {})]
        replies.extend([response({}), response(None), response([]),
                        response({"register": False}, status=403),
                        admin.Response(200, Message(), b"not-json")])
        for index, reply in enumerate(replies):
            with self.subTest(case=index), mock.patch.object(admin.secrets, "token_hex") as address, \
                    mock.patch.object(admin.secrets, "token_urlsafe") as password:
                client = client_with(reply)
                self.assert_failure(client)
                client.request.assert_called_once_with("GET", "/api/auth/can-register")
                address.assert_not_called()
                password.assert_not_called()

    def test_requires_supported_empty_cookiejar_without_clearing_existing_cookies(self):
        jars = [[], {}, None]
        for name in ("auth", "org", "unrelated"):
            jar = http.cookiejar.CookieJar()
            cookie = auth_cookie()
            cookie.name = name
            jar.set_cookie(cookie)
            jars.append(jar)
        for index, jar in enumerate(jars):
            with self.subTest(case=index):
                client = self.closed_client()
                client.cookies = jar
                self.assert_failure(client)
                client.request.assert_not_called()
                self.assertIs(client.cookies, jar)
                if isinstance(jar, http.cookiejar.CookieJar):
                    self.assertEqual(len(jar), 1)

    def test_policy_get_must_not_seed_cookies_before_post(self):
        for name in ("auth", "org", "unrelated"):
            with self.subTest(cookie=name):
                client = self.closed_client()

                def request(*args, **kwargs):
                    cookie = auth_cookie()
                    cookie.name = name
                    client.cookies.set_cookie(cookie)
                    return response({"register": False})

                client.request.side_effect = request
                self.assert_failure(client)
                client.request.assert_called_once_with("GET", "/api/auth/can-register")
                self.assertEqual(len(client.cookies), 1)

    def test_each_call_uses_distinct_synthetic_dto_valid_payload(self):
        payloads = []
        with mock.patch.object(admin.secrets, "token_hex", wraps=admin.secrets.token_hex) as address, \
                mock.patch.object(admin.secrets, "token_urlsafe", wraps=admin.secrets.token_urlsafe) as password:
            for _ in range(2):
                client = self.closed_client()
                admin.postiz_registration_denial(client)
                payload = client.request.call_args_list[1].kwargs["data"]
                payloads.append(payload)
                self.assertEqual(set(payload), {"email", "password", "provider", "company"})
                self.assertTrue(re.fullmatch(r"business-tools-signup-check-[0-9a-f]{32}@example\.invalid",
                                             payload["email"]) is not None)
                self.assertLessEqual(len(payload["email"].split("@")[0]), 64)
                self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{43}", payload["password"]) is not None)
                self.assertEqual(payload["provider"], "LOCAL")
                self.assertEqual(payload["company"], "Security check")
            self.assertEqual(address.call_args_list, [mock.call(16), mock.call(16)])
            self.assertEqual(password.call_args_list, [mock.call(32), mock.call(32)])
        for key in ("email", "password"):
            self.assertEqual(len({payload[key] for payload in payloads}), 2)

    def test_only_exact_http400_plain_text_is_denial_no_cleanup_on_success(self):
        # Tagged v2.23.0 auth.controller.ts uses .status(400).send(e.message).
        # JSON-looking denials, including an exact message field, must fail closed.
        replies = [response(payload, status=400) for payload in (
            {"message": "Registration is disabled"},
            {"message": "Email already exists"}, {"message": ["email must be an email"]},
            {"message": "Registration is disabled "}, {"message": ["Registration is disabled"]},
            {"error": "Registration is disabled"}, {}, None, [], "Registration is disabled",
        )]
        replies.extend(admin.Response(400, Message(), body) for body in (
            b"", b"Email already exists", b"Registration is disabled ",
            b" Registration is disabled", b"Registration is disabled\n",
            b"registration is disabled", "Registration is disabled",
        ))
        replies.extend(admin.Response(status, Message(), b"Registration is disabled")
                       for status in (200, 201, 302, 401, 403, 500, "400", "400 Bad Request"))
        replies.append(response({"register": True}, status=200))
        for index, reply in enumerate(replies):
            with self.subTest(case=index):
                client = client_with(response({"register": False}), reply)
                self.assert_failure(client)
                self.assertEqual([call.args for call in client.request.call_args_list], [
                    ("GET", "/api/auth/can-register"), ("POST", "/api/auth/register")])

    def test_policy_must_stay_strictly_false_after_denial(self):
        replies = [response({"register": value}) for value in (True, "false", None, 0)]
        replies.extend([response({}), response(None), response({"register": False}, status=500)])
        for index, reply in enumerate(replies):
            with self.subTest(case=index):
                client = client_with(response({"register": False}),
                                     admin.Response(400, Message(), b"Registration is disabled"), reply)
                self.assert_failure(client)
                self.assertEqual([call.args for call in client.request.call_args_list], [
                    ("GET", "/api/auth/can-register"), ("POST", "/api/auth/register"),
                    ("GET", "/api/auth/can-register")])

    def test_auth_cookie_on_denial_or_recheck_fails_and_is_not_cleared(self):
        for cookie_at in (2, 3):
            with self.subTest(cookie_at=cookie_at):
                client = self.closed_client()

                def request(method, path, **kwargs):
                    if client.request.call_count == cookie_at:
                        client.cookies.set_cookie(auth_cookie())
                    if method == "POST":
                        return admin.Response(400, Message(), b"Registration is disabled")
                    return response({"register": False})

                client.request.side_effect = request
                self.assert_failure(client)
                self.assertEqual(client.request.call_count, cookie_at)
                self.assertEqual(len(client.cookies), 1)

    def test_response_and_transport_exceptions_do_not_expose_generated_secrets(self):
        for failure in ("transport", "body"):
            with self.subTest(failure=failure):
                client = self.closed_client()
                private_values = []

                def request(method, path, data=None):
                    if method == "GET":
                        return response({"register": False})
                    private_values.extend([data["email"], data["password"], TOKEN])
                    if failure == "transport":
                        raise OSError(" ".join(private_values))
                    return admin.Response(400, Message(), " ".join(private_values).encode())

                client.request.side_effect = request
                stdout, stderr, diagnostic = io.StringIO(), io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    try:
                        admin.postiz_registration_denial(client)
                    except RuntimeError as error:
                        self.assertEqual(str(error), admin.ERROR)
                        traceback.print_exception(error, file=diagnostic)
                    else:
                        self.fail("Expected generic private-operation failure")
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")
                self.assertTrue(all(value not in diagnostic.getvalue() for value in private_values))
                self.assertEqual(client.request.call_count, 2)


class TransportTests(OfflineTests):
    def wire_socket(self, status=200, headers="", body=b"{}"):
        sock = mock.Mock()
        wire = (f"HTTP/1.1 {status} Synthetic\r\nContent-Length: {len(body)}\r\n"
                f"{headers}\r\n").encode() + body
        sock.makefile.return_value = io.BytesIO(wire)
        return sock

    def test_canonical_host_sni_verified_tls_and_only_loopback_tcp(self):
        for host in admin.HOSTS.values():
            with self.subTest(host=host):
                raw, tls = mock.Mock(), self.wire_socket()
                with mock.patch.object(admin.socket, "create_connection", return_value=raw) as connect, \
                        mock.patch.object(ssl.SSLContext, "wrap_socket", autospec=True, return_value=tls) as wrap, \
                        mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.invalid:8080",
                                                     "ALL_PROXY": "http://proxy.example.invalid:8080"}):
                    client = admin.Client(host)
                    self.assertEqual(client.request("GET", "/health").status, 200)
                connect.assert_called_once_with(("127.0.0.1", 18443), 90)
                context = wrap.call_args.args[0]
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                wrap.assert_called_once_with(context, raw, server_hostname=host)
                wire = b"".join(call.args[0] for call in tls.sendall.call_args_list)
                self.assertIn(b"GET /health HTTP/1.1\r\n", wire)
                self.assertIn(("\r\nHost: " + host + "\r\n").encode(), wire)
                self.assertEqual(client.origin, "https://" + host)
                self.assertNotIn(b"127.0.0.1", wire)
                self.assertNotIn(b":18443", wire)
                tls.close.assert_called_once()

    def test_postiz_denial_uses_fresh_cookiejar_loopback_only_and_extracts_auth_cookies(self):
        for cookie_at in (None, 2, 3):
            with self.subTest(cookie_at=cookie_at):
                sockets = [self.wire_socket(
                    status=400 if step == 2 else 200,
                    headers=("Set-Cookie: auth=" + TOKEN + "; Path=/; Secure; HttpOnly\r\n"
                             if step == cookie_at else ""),
                    body=(b"Registration is disabled" if step == 2
                          else json.dumps({"register": False}).encode()),
                ) for step in (1, 2, 3)]
                with mock.patch.object(admin.socket, "create_connection", return_value=mock.Mock()) as connect, \
                        mock.patch.object(ssl.SSLContext, "wrap_socket", side_effect=sockets) as wrap:
                    client = admin.Client(admin.HOSTS["postiz"], 19443)
                    self.assertIsInstance(client.cookies, http.cookiejar.CookieJar)
                    self.assertEqual(len(client.cookies), 0)
                    if cookie_at is None:
                        self.assertEqual(admin.postiz_registration_denial(client),
                                         {"api_second_signup_denied": True})
                    else:
                        with self.assertRaises(RuntimeError) as caught:
                            admin.postiz_registration_denial(client)
                        self.assertEqual(str(caught.exception), admin.ERROR)
                count = cookie_at or 3
                self.assertEqual(connect.call_args_list, [mock.call(("127.0.0.1", 19443), 90)] * count)
                self.assertEqual(wrap.call_count, count)
                self.assertTrue(all(call.kwargs["server_hostname"] == admin.HOSTS["postiz"]
                                    for call in wrap.call_args_list))
                for index, tls in enumerate(sockets[:count]):
                    wire = b"".join(call.args[0] for call in tls.sendall.call_args_list)
                    expected = (b"POST /api/auth/register HTTP/1.1" if index == 1
                                else b"GET /api/auth/can-register HTTP/1.1")
                    self.assertEqual(wire.split(b"\r\n", 1)[0], expected)
                    headers = wire.split(b"\r\n\r\n", 1)[0].lower()
                    self.assertTrue(b"\r\nhost: " + client.host.encode() + b"\r\n" in headers + b"\r\n")
                    self.assertTrue(b"\r\ncookie:" not in headers and b"\r\nauthorization:" not in headers)
                    tls.close.assert_called_once()
                self.assertEqual(len(client.cookies), int(cookie_at is not None))

    def test_redirects_are_returned_without_following_or_replaying(self):
        for status in (301, 302, 303, 307, 308):
            for location in ("/other", "https://external.example.invalid/steal", "//external.example.invalid/steal"):
                with self.subTest(status=status, location=location):
                    tls = self.wire_socket(status, "Location: " + location + "\r\n")
                    with mock.patch.object(admin.socket, "create_connection") as connect, \
                            mock.patch.object(ssl.SSLContext, "wrap_socket", return_value=tls):
                        reply = admin.Client(admin.HOSTS["postiz"]).request("POST", "/login", data={"password": TOKEN})
                    self.assertEqual(reply.status, status)
                    self.assertEqual(reply.headers["Location"], location)
                    connect.assert_called_once_with(("127.0.0.1", 18443), 90)
                    wire = b"".join(call.args[0] for call in tls.sendall.call_args_list)
                    self.assertEqual(wire.count(b"POST /login HTTP/1.1"), 1)
                    self.assertNotIn(b"GET ", wire)
                    tls.close.assert_called_once()

    def test_failed_certificate_closes_socket_without_public_fallback(self):
        raw = mock.Mock()
        with mock.patch.object(admin.socket, "create_connection", return_value=raw) as connect, \
                mock.patch.object(ssl.SSLContext, "wrap_socket", side_effect=ssl.SSLCertVerificationError("synthetic TLS failure")):
            with self.assertRaises(ssl.SSLCertVerificationError):
                admin.Client(admin.HOSTS["grafana"], 19443).request("GET", "/api/user")
        connect.assert_called_once_with(("127.0.0.1", 19443), 90)
        raw.close.assert_called_once()

    def test_rejects_noncanonical_hosts_ports_and_unsafe_requests_before_connect(self):
        for host, port in (("external.example.invalid", 18443), ("127.0.0.1", 18443),
                           (admin.HOSTS["actual"], 443), (admin.HOSTS["actual"], True),
                           (admin.HOSTS["actual"], 65536)):
            with self.subTest(host=host, port=port), self.assertRaises(RuntimeError):
                admin.Client(host, port)
        client = admin.Client(admin.HOSTS["actual"])
        for path in ("https://external.example.invalid/", "//external.example.invalid/", "/\\outside", "/bad\n", "/x#y"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                client.request("GET", path)
        for headers in ({"Host": "external.example.invalid"}, {"hOsT": "other"}, {"Cookie": TOKEN}):
            with self.assertRaises(RuntimeError):
                client.request("GET", "/", headers=headers)
        with self.assertRaises(RuntimeError):
            client.request("DELETE", "/")
        with self.assertRaises(RuntimeError):
            client.request("POST", "/", data={}, form={})


class ProtectedJsonTests(OfflineTests):
    def setUp(self):
        super().setUp()
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="private-admin-test-"))
        # macOS temp roots may include /var symlinks; only our explicit symlink
        # cases should exercise refusal, not the platform's temp alias.
        self.root = Path(temporary).resolve()
        self.root.chmod(0o700)
        self.path = self.root / "synthetic.json"
        self.path.write_text(json.dumps({"actual": {"password": TOKEN}}))
        self.path.chmod(0o600)

    def assert_unreadable(self, path):
        with mock.patch.object(admin.json, "load") as load:
            with self.assertRaises((RuntimeError, OSError)):
                admin.protected_json(path)
            load.assert_not_called()

    def test_owner_only_regular_file_is_accepted_without_changing_it(self):
        before = self.path.read_bytes(), self.path.stat()
        self.assertEqual(admin.protected_json(self.path), {"actual": {"password": TOKEN}})
        after = self.path.stat()
        self.assertEqual(self.path.read_bytes(), before[0])
        self.assertEqual((after.st_ino, after.st_mode, after.st_mtime_ns),
                         (before[1].st_ino, before[1].st_mode, before[1].st_mtime_ns))

    def test_group_or_world_permissions_are_rejected_before_json_read(self):
        for target, modes in ((self.path, (0o604, 0o640, 0o660, 0o644)),
                              (self.root, (0o701, 0o710, 0o750, 0o755))):
            for mode in modes:
                with self.subTest(target=target.name, mode=oct(mode)):
                    self.path.chmod(0o600)
                    self.root.chmod(0o700)
                    target.chmod(mode)
                    self.assert_unreadable(self.path)

    def test_leaf_symlink_and_symlinked_parent_are_rejected(self):
        leaf = self.root / "linked.json"
        leaf.symlink_to(self.path)
        self.assert_unreadable(leaf)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_unreadable(alias / self.path.name)

    def test_hardlink_rejects_both_names(self):
        linked = self.root / "hardlinked.json"
        os.link(self.path, linked)
        self.assert_unreadable(self.path)
        self.assert_unreadable(linked)

    def test_nonregular_file_and_wrong_file_owner_are_rejected(self):
        directory = self.root / "not-a-file"
        directory.mkdir(mode=0o700)
        self.assert_unreadable(directory)
        info = self.path.stat()
        wrong_owner = mock.Mock(st_mode=info.st_mode, st_nlink=1, st_uid=os.geteuid() + 1)
        with mock.patch.object(admin.os, "fstat", return_value=wrong_owner):
            self.assert_unreadable(self.path)


class OutputSafetyTests(OfflineTests):
    def test_credentials_transport_and_response_failures_only_print_fixed_error(self):
        private = "synthetic-private-response-cookie-password"
        for stage in ("credentials", "transport", "response", "json"):
            with self.subTest(stage=stage):
                client = client_with()
                if stage == "transport":
                    failure = OSError(private)
                elif stage == "json":
                    failure = admin.Response(200, Message(), private.encode())
                else:
                    failure = response({"private": private}, status=500)
                # Fail after credentials were submitted and a session cookie
                # was obtained, not just at the initial unauthenticated probe.
                client.cookies.set_cookie(auth_cookie())
                client.request.side_effect = [response({"register": False}), response({"login": True}), failure]
                with mock.patch.object(admin, "Client", return_value=client), \
                        mock.patch.object(admin, "protected_json", return_value={"postiz": CREDENTIALS}) as loader:
                    if stage == "credentials":
                        loader.side_effect = OSError(private)
                    self.assertEqual(self.cli(["--app", "postiz", "--apply", "--credentials", "/synthetic/file.json"]),
                                     (1, admin.ERROR + "\n"))
                if stage == "credentials":
                    client.request.assert_not_called()
                else:
                    self.assertEqual([call.args for call in client.request.call_args_list], [
                        ("GET", "/api/auth/can-register"), ("POST", "/api/auth/login"), ("GET", "/api/user/self")])

    def test_response_repr_does_not_include_body_or_cookie_values(self):
        item = response({"private": TOKEN})
        item.headers["Set-Cookie"] = "auth=" + TOKEN
        self.assertNotIn(TOKEN, repr(item))

    def test_success_prints_only_safe_summary_not_credentials_or_response_fields(self):
        client = client_with(response(status=401), response(status=404), response({"data": [{
            "is_owner": True, "is_admin": True, "token": TOKEN, "user": {"email": CREDENTIALS["email"]}}]}))
        with mock.patch.object(admin, "Client", return_value=client), \
                mock.patch.object(admin, "protected_json", return_value={"invoice_ninja": CREDENTIALS}) as loader:
            code, output = self.cli(["--app", "invoice_ninja", "--apply", "--credentials", "/synthetic/file.json"])
        self.assertEqual(code, 0)
        loader.assert_called_once_with(Path("/synthetic/file.json"))
        self.assertEqual(json.loads(output), {"app": "invoice_ninja", "login_verified": True,
                         "owner_and_administrator": True, "anonymous_clients_denied": True, "setup_unavailable": True})
        for value in (*CREDENTIALS.values(), TOKEN):
            self.assertNotIn(value, output)


if __name__ == "__main__":
    unittest.main()