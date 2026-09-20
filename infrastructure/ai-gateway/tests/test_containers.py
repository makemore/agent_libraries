"""Opt-in, local-only smoke test of configured digests; never print Docker output."""

from contextlib import ExitStack
import base64
import http.client
from http.cookies import SimpleCookie
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from test_policy import POST_ROUTES, ROOT, image_pins


def docker(operation, *arguments):
    try:
        result = subprocess.run(["docker", operation, *arguments], capture_output=True,
                                text=True, timeout=300 if operation == "pull" else 45)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Docker " + operation + " failed (exit code unavailable)") from None
    if result.returncode:
        raise RuntimeError(f"Docker {operation} failed (exit code {result.returncode})")
    return result.stdout


def published_port(name, port):
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]+)", docker("port", name, f"{port}/tcp").strip())
    if match is None:
        raise RuntimeError("Docker port returned an unexpected binding")
    return int(match[1])


def status(port, path, method="GET", headers=None, payload=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        body = json.dumps(payload or {"model": "openai/smoke-test", "input": "test",
                                     "messages": [{"role": "user", "content": "test"}], "max_tokens": 1}).encode()
        connection.request(method, path, body if method == "POST" else None,
                           {"Content-Type": "application/json", **(headers or {})})
        return connection.getresponse().status  # Never read/log response content or auth headers.
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        connection.close()


@unittest.skipUnless(os.environ.get("GATEWAY_CONTAINER_TESTS") == "1", "Opt-in Docker smoke test")
class ContainerTests(unittest.TestCase):
    def test_pinned_gateway_rejects_untrusted_requests(self):
        pins = image_pins()
        for image in pins.values():
            docker("pull", image)  # Native architecture; keep the downloaded images.
        with tempfile.TemporaryDirectory(prefix="gateway-smoke-", dir="/tmp") as temporary, ExitStack() as cleanup:
            root = Path(temporary)
            spec = importlib.util.spec_from_file_location("smoke_secrets", ROOT / "runtime/fetch-secrets.py")
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            env = root / "private/bootstrap.env"
            credentials = {key: secrets.token_urlsafe(32) for key in helper.KEYS}
            helper.write_env(credentials, env)
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            data = root / "data"
            data.mkdir()
            config = data / "config.json"
            config.write_bytes((ROOT / "runtime/config.json").read_bytes())
            # Isolated Linux UID mismatch/macOS bind mounts only; private temp parent stays 0700.
            # Removed with this test's temp tree, never applied to production/user data.
            data.chmod(0o777)
            config.chmod(0o666)
            bifrost, caddy = ("gateway-smoke-" + secrets.token_hex(12) for _ in range(2))
            hardened = ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true")
            cleanup.callback(docker, "rm", "-f", bifrost)
            # Test-only bind override for publishing; production retains loopback + host networking.
            docker("run", "-d", "--name", bifrost, *hardened, "--user=1000:1000",
                   "--memory=2560m", "--pids-limit=256", "--tmpfs=/tmp:size=64m,mode=1777",
                   "--env-file", str(env), "-e", "APP_HOST=0.0.0.0", "-e", "APP_PORT=8080",
                   "-e", "APP_DIR=/app/data", "-e", "LOG_LEVEL=warn", "-e", "GOMEMLIMIT=2300MiB",
                   "-v", f"{data}:/app/data", "-p", "127.0.0.1::8080", "-p", "127.0.0.1::9080", pins["bifrost"])
            cleanup.callback(docker, "rm", "-f", caddy)
            # Local HTTP only: exercises real Caddy allowlist, not production TLS issuance.
            docker("run", "-d", "--name", caddy, *hardened, "--network", "container:" + bifrost,
                   "--cap-add=NET_BIND_SERVICE",  # Match production and the image's file capability.
                   "--tmpfs=/tmp", "--tmpfs=/data", "--tmpfs=/config", "--memory=256m",
                   "-e", "GATEWAY_DOMAIN=http://:9080", "-v", f"{ROOT}/runtime/Caddyfile:/etc/caddy/Caddyfile:ro",
                   pins["caddy"])
            backend, frontend = (published_port(bifrost, port) for port in (8080, 9080))
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                backend_status = status(backend, "/health")
                frontend_status = status(frontend, "/healthz")
                if backend_status == 200 and frontend_status == 200:
                    break
                time.sleep(0.5)
            else:
                self.fail(f"Readiness timed out: backend={backend_status}, frontend={frontend_status}")
            self.assertTrue((data / "config.db").is_file(), "Persistent config DB must initialize")
            self.assertFalse((data / "logs.db").exists(), "Request logging must remain disabled")
            self.assertIn(status(backend, "/api/config"), (401, 403))
            # Exercise the supported management API. Test-only provider credentials target
            # an unreachable loopback port, never a real provider or a paid model call.
            admin_token = base64.b64encode((credentials["BIFROST_ADMIN_USERNAME"] + ":" +
                                           credentials["BIFROST_ADMIN_PASSWORD"]).encode()).decode()
            admin = {"Authorization": "Basic " + admin_token}
            self.assertEqual(status(backend, "/api/config", headers=admin), 200)
            for provider in ("openai", "anthropic"):
                payload = {"provider": provider,
                           "network_config": {"base_url": "http://127.0.0.1:1", "allow_private_network": True},
                           "keys": [{"name": "isolated-smoke", "value": secrets.token_urlsafe(32),
                                     "models": ["smoke-test"], "weight": 1.0}]}
                self.assertIn(status(backend, "/api/providers", "POST", admin, payload), (200, 201))
            self.assertEqual(status(frontend, "/healthz"), 200)
            fake = {"Authorization": "Bearer sk-bf-" + secrets.token_urlsafe(32)}
            for path in ("/", "/api/config", "/metrics", "/mcp", "/v1/responses/id", "/v1/files", "/v1/messages/batches"):
                for method in ("GET", "POST"):
                    with self.subTest(blocked_path=path, method=method):
                        self.assertEqual(status(frontend, path, method, fake), 404)
            self.assertEqual(status(frontend, "/v1/models", "POST", fake), 404)
            # Named unavailable-guard exception: this smoke fixture intentionally
            # has no listener on 9092. The production GET route must fail closed,
            # never fall through to Bifrost; this fixture is removed on exit.
            # Actual guarded catalogs/auth are proved in test_model_discovery.py.
            self.assertEqual(status(frontend, "/v1/models", headers=fake), 502)
            bypass = {**fake, "x-bf-vk": "sk-bf-" + secrets.token_urlsafe(32),
                      "x-bf-direct-key": "true", "x-api-key": secrets.token_urlsafe(32)}
            malformed = {**bypass, "Authorization": "Bearer " + secrets.token_urlsafe(32)}
            for method, path in (("POST", path) for path in POST_ROUTES):
                with self.subTest(method=method, path=path):
                    self.assertEqual(status(frontend, path, method), 401)
                    self.assertIn(status(frontend, path, method, fake), (401, 403))
                    self.assertIn(status(frontend, path, method, bypass), (401, 403))
                    self.assertEqual(status(frontend, path, method, malformed), 401)
                    other_method = "POST" if method == "GET" else "GET"
                    self.assertEqual(status(frontend, path, other_method, fake), 404)


@unittest.skipUnless(os.environ.get("GATEWAY_ADMIN_CONTAINER_TESTS") == "1", "Opt-in Docker admin smoke test")
class AdminContainerTests(unittest.TestCase):
    def test_pinned_admin_requires_iap_and_bifrost_auth(self):
        # Run with the root .venv Python and Docker Desktop's host.docker.internal
        # loopback forwarding. Optional crypto dependencies stay out of default discovery.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import jwt

        pins = image_pins()
        for image in pins.values():
            docker("pull", image)
        with tempfile.TemporaryDirectory(prefix="gateway-admin-smoke-", dir="/tmp") as temporary, ExitStack() as cleanup:
            root = Path(temporary)
            spec = importlib.util.spec_from_file_location("admin_smoke_secrets", ROOT / "runtime/fetch-secrets.py")
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            env = root / "private/bootstrap.env"
            credentials = {key: secrets.token_urlsafe(32) for key in helper.KEYS}
            helper.write_env(credentials, env)
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
            data = root / "data"
            data.mkdir()
            config = data / "config.json"
            config.write_bytes((ROOT / "runtime/config.json").read_bytes())
            # Isolated Linux UID mismatch/macOS bind mounts only; private parent
            # stays 0700. Removed with the temp tree, never applied to user data.
            data.chmod(0o777)
            config.chmod(0o666)

            spec = importlib.util.spec_from_file_location("admin_smoke_iap_auth", ROOT / "runtime/iap-auth.py")
            auth = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(auth)
            private_key = ec.generate_private_key(ec.SECP256R1())
            other_key = ec.generate_private_key(ec.SECP256R1())
            public_key = private_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")
            # Only the key source is mocked: exercise the real cache, signature
            # verifier and HTTP server. No live Google key fetch is possible.
            fetch = cleanup.enter_context(mock.patch.object(auth, "fetch_public_keys", return_value={"smoke-key": public_key}))
            audience = "/projects/123456789/global/backendServices/987654321"
            email = "operator@example.invalid"
            verifier = auth.Verifier({"enabled": True, "audience": audience, "allowed_emails": [email]}, auth.KeyCache())
            auth_lifetime = cleanup.enter_context(ExitStack())
            with mock.patch.object(auth, "LISTEN_ADDRESS", ("127.0.0.1", 0)):
                server = auth_lifetime.enter_context(auth.AuthServer(verifier))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            auth_lifetime.callback(thread.join, timeout=5)
            auth_lifetime.callback(server.shutdown)
            auth_port = server.server_address[1]

            admin_host = {"Host": "admin.example.invalid"}
            template = (ROOT / "templates/admin.caddy.tftpl").read_text()
            self.assertEqual(template.count("127.0.0.1:9091"), 1)
            # Test-only host bridge; production keeps the verifier on loopback.
            # Both replacements exist only in this disposable rendered template.
            rendered = template.replace("${admin_domain}", admin_host["Host"]).replace(
                "127.0.0.1:9091", f"host.docker.internal:{auth_port}",
            )
            self.assertFalse("${" in rendered, "Admin template must be fully rendered")
            admin_config = root / "admin.caddy"
            admin_config.write_text(rendered)
            bifrost, caddy = ("gateway-admin-smoke-" + secrets.token_hex(12) for _ in range(2))
            hardened = ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true")
            cleanup.callback(docker, "rm", "-f", bifrost)
            # Test-only bind override for publishing; production stays loopback.
            # Preserve real config/auth defaults; no providers or inference calls.
            docker("run", "-d", "--name", bifrost, *hardened, "--user=1000:1000",
                   "--memory=2560m", "--pids-limit=256", "--tmpfs=/tmp:size=64m,mode=1777",
                   "--env-file", str(env), "-e", "APP_HOST=0.0.0.0", "-e", "APP_PORT=8080",
                   "-e", "APP_DIR=/app/data", "-e", "LOG_LEVEL=warn", "-e", "GOMEMLIMIT=2300MiB",
                   "-v", f"{data}:/app/data", "-p", "127.0.0.1::8080", "-p", "127.0.0.1::8081", pins["bifrost"])
            cleanup.callback(docker, "rm", "-f", caddy)
            docker("run", "-d", "--name", caddy, *hardened, "--network", "container:" + bifrost,
                   "--cap-add=NET_BIND_SERVICE", "--tmpfs=/tmp", "--tmpfs=/data", "--tmpfs=/config", "--memory=256m",
                   "-e", "GATEWAY_DOMAIN=http://:9080", "-v", f"{ROOT}/runtime/Caddyfile:/etc/caddy/Caddyfile:ro",
                   "-v", f"{admin_config}:/etc/caddy/admin.d/admin.caddy:ro", pins["caddy"])
            backend, frontend = (published_port(bifrost, port) for port in (8080, 8081))
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                backend_status = status(backend, "/health")
                frontend_status = status(frontend, "/_iap_health", headers=admin_host)
                if backend_status == 200 and frontend_status == 200:
                    break
                time.sleep(0.5)
            else:
                self.fail(f"Readiness timed out: backend={backend_status}, frontend={frontend_status}")

            now = int(time.time())
            claims = {"sub": "synthetic-subject", "email": email, "iss": auth.ISSUER,
                      "aud": audience, "iat": now - 10, "exp": now + 590}
            valid = {auth.ASSERTION_HEADER: jwt.encode(claims, private_key, algorithm="ES256", headers={"kid": "smoke-key"})}
            forged = {auth.ASSERTION_HEADER: jwt.encode(claims, other_key, algorithm="ES256", headers={"kid": "smoke-key"})}
            admin_token = base64.b64encode((credentials["BIFROST_ADMIN_USERNAME"] + ":" +
                                           credentials["BIFROST_ADMIN_PASSWORD"]).encode()).decode()
            basic = {"Authorization": "Basic " + admin_token}
            authenticated = {**admin_host, **valid, **basic}
            self.assertEqual(status(frontend, "/", headers={**admin_host, **valid}), 200)
            config_paths = ("/api/config", "/api/config?from_db=false", "/api/config?from_db=true")
            for path in config_paths:
                with self.subTest(config_path=path):
                    self.assertEqual(status(frontend, path, headers={**admin_host, **valid}), 401)
                    self.assertEqual(status(frontend, path, headers=authenticated), 200)

            # Browser flow: use Bifrost's actual login and cookie, not Basic auth.
            # The dashboard always fetches config with ?from_db=false. That query
            # must not leak into the verifier's exact /verify request target.
            connection = http.client.HTTPConnection("127.0.0.1", frontend, timeout=10)
            try:
                connection.request("POST", "/api/session/login", json.dumps({
                    "username": credentials["BIFROST_ADMIN_USERNAME"],
                    "password": credentials["BIFROST_ADMIN_PASSWORD"],
                }), {**admin_host, **valid, "Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                cookie = SimpleCookie()
                cookie.load(response.getheader("Set-Cookie", ""))
                response.read()
                self.assertTrue("token" in cookie, "Login must set a session cookie")
                self.assertTrue(cookie["token"]["secure"] and cookie["token"]["httponly"])
                browser = {**admin_host, **valid, "Cookie": "token=" + cookie["token"].value}
                try:
                    connection.request("GET", "/api/config?from_db=false", headers=browser)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.getheader("Content-Type", "").startswith("application/json"))
                    config = json.loads(response.read())
                    self.assertIs(config.get("is_db_connected"), True)
                    self.assertIs(config.get("is_logs_connected"), False)
                finally:
                    self.assertEqual(status(frontend, "/api/session/logout", "POST", browser, {}), 200)
                self.assertEqual(status(frontend, "/api/config?from_db=false", headers=browser), 401)
            finally:
                connection.close()

            unsigned = {"X-Goog-Authenticated-User-Email": "accounts.google.com:" + email,
                        "X-Goog-Authenticated-User-Id": "accounts.google.com:synthetic-subject"}
            # Labels, never token/header values, appear in subtest/assert output.
            for case, assertion in (("missing", {}), ("fake", {auth.ASSERTION_HEADER: "not-a-jwt"}), ("forged", forged)):
                for bypass, headers in (("none", {}), ("basic", basic), ("unsigned", unsigned), ("both", {**basic, **unsigned})):
                    for path in ("/", *config_paths):
                        with self.subTest(assertion=case, bypass=bypass, path=path):
                            self.assertEqual(status(frontend, path, headers={**admin_host, **assertion, **headers}), 401)
            for path in ("/", *config_paths, "/_iap_health"):
                with self.subTest(wrong_host_path=path):
                    self.assertEqual(status(frontend, path, headers={**authenticated, "Host": "wrong.example.invalid"}), 404)
            self.assertEqual(status(frontend, "/_iap_health", headers=admin_host), 200)
            self.assertEqual(fetch.call_count, 1)

            # Stop the real verifier and close its listener: valid IAP + Basic
            # must not fall through to Bifrost. Require an actual HTTP failure,
            # not status()'s zero sentinel for an unavailable Caddy listener.
            auth_lifetime.close()
            self.assertFalse(thread.is_alive(), "Verifier thread must stop")
            for path in ("/", *config_paths):
                with self.subTest(unavailable_verifier_path=path):
                    result = status(frontend, path, headers=authenticated)
                    self.assertGreaterEqual(result, 500)
                    self.assertLess(result, 600)
            self.assertEqual(status(frontend, "/_iap_health", headers=admin_host), 200)
            # Deliberately caused proxy errors must not log bearer assertions,
            # Bifrost credentials or sensitive request headers/URLs.
            logs = subprocess.run(["docker", "logs", caddy], capture_output=True, text=True, timeout=20)
            self.assertEqual(logs.returncode, 0)
            text = logs.stdout + logs.stderr
            for private_value in (valid[auth.ASSERTION_HEADER], admin_token):
                self.assertFalse(private_value in text, "Proxy error logs must redact authentication values")
            for line in text.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                request = event.get("request", {})
                self.assertFalse("headers" in request or "uri" in request, "Request metadata must be redacted")