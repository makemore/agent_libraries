"""Static gateway policy checks; no cloud, Docker, or credential access."""

import ast
import configparser
import http.client
import json
from pathlib import Path
import re
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
POST_ROUTES = ("/v1/chat/completions", "/v1/responses", "/v1/embeddings", "/anthropic/v1/messages")


def image_pins():
    pins = dict(re.findall(r'^(bifrost|caddy)_image\s*=\s*"([^"]+)"\s*$',
                           (ROOT / "terraform.tfvars.example").read_text(), re.M))
    for name, repository in (("bifrost", "maximhq/bifrost"), ("caddy", "caddy")):
        if not re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", pins.get(name, "")):
            raise RuntimeError("Invalid example image pin")
    return pins


class PolicyTests(unittest.TestCase):
    def test_private_unseeded_config(self):
        config = json.loads((ROOT / "runtime/config.json").read_text())
        self.assertEqual(config.get("version", 2), 2, "Never enable legacy allow-all semantics")
        client, governance = config["client"], config["governance"]
        for key in ("enforce_auth_on_inference", "disable_content_logging"):
            self.assertIs(client[key], True, key)
        for key in ("allow_direct_keys", "enable_logging", "allow_per_request_content_storage_override",
                    "allow_per_request_raw_override", "dump_errors_in_console_logs"):
            self.assertIs(client[key], False, key)
        self.assertIs(governance["auth_config"]["is_enabled"], True)
        self.assertIs(governance["auth_config"]["disable_auth_on_inference"], False)
        # v2 may omit unused stores/collections rather than seed empty legacy entries.
        self.assertIs(config.get("logs_store", {}).get("enabled", False), False)
        for section in (config, governance):
            for key in ("providers", "virtual_keys"):
                self.assertTrue(not section.get(key), "No seeded " + key)

    def test_pinned_compose_and_loopback(self):
        self.assertEqual(set(image_pins()), {"bifrost", "caddy"})
        compose = (ROOT / "templates/compose.yaml.tftpl").read_text()
        self.assertEqual(re.findall(r'^\s+image: "([^"]+)"$', compose, re.M),
                         ["${bifrost_image}", "${caddy_image}"])
        self.assertFalse(re.search(r"^\s*(restart|restart_policy|ports):", compose, re.M))
        self.assertEqual(compose.count("network_mode: host"), 2)
        self.assertTrue('APP_HOST: "127.0.0.1"' in compose)
        for setting in ("read_only: true", "cap_drop: [ALL]", "security_opt: [no-new-privileges:true]"):
            self.assertEqual(compose.count(setting), 2)

    def test_exact_caddy_allowlist_and_header_filter(self):
        caddy = (ROOT / "runtime/Caddyfile").read_text()
        self.assertLess(caddy.index("(keyed_inference)"), caddy.index("import keyed_inference"))
        paths = re.findall(r"^\s*path (.+)$", caddy, re.M)
        self.assertEqual([path for line in paths for path in line.split()],
                         ["/healthz", "/v1/models", *POST_ROUTES])
        for name, method in (("health", "GET"), ("model_catalog", "GET"), ("inference", "POST")):
            self.assertTrue(re.search(r"@" + name + r"\s*\{\s*method " + method + r"\s+path ", caddy))
        catalog = caddy.split("handle @model_catalog {", 1)[1].split("\n\t}", 1)[0]
        self.assertIn("reverse_proxy 127.0.0.1:9092", catalog)
        self.assertNotIn("keyed_inference", catalog)
        self.assertNotIn("127.0.0.1:8080", catalog)
        self.assertTrue('"^Bearer sk-bf-[A-Za-z0-9_-]+$"' in caddy)
        self.assertTrue('respond "Not found" 404' in caddy)
        self.assertTrue('respond "Gateway virtual key required" 401' in caddy)
        # Only the global error-log redaction is configured, never access logging.
        self.assertEqual(len(re.findall(r"^\s*log(?:\s|$)", caddy, re.M)), 1)
        self.assertIn("request>headers delete", caddy)
        self.assertIn("request>uri delete", caddy)
        for header in ("X-Bf-*", "Cookie", "X-Api-Key", "X-Goog-Api-Key", "Api-Key", "Proxy-Authorization"):
            self.assertTrue("header_up -" + header in caddy)

    def test_admin_is_separate_and_authenticates_before_proxying(self):
        admin = (ROOT / "templates/admin.caddy.tftpl").read_text()
        self.assertIn("http://:8081", admin)
        self.assertIn("@admin host ${admin_domain}", admin)
        self.assertLess(admin.index("forward_auth 127.0.0.1:9091"), admin.index("reverse_proxy 127.0.0.1:8080"))
        self.assertIn("uri /verify?\n", admin)
        self.assertIn("header_up X-Forwarded-Proto https", admin)
        self.assertNotIn("BIFROST_ADMIN_PASSWORD", admin)
        service = (ROOT / "runtime/iap-auth.service").read_text()
        for setting in ("DynamicUser=true", "ProtectSystem=strict", "NoNewPrivileges=true", "ProtectHome=true"):
            self.assertIn(setting, service)


class ModelCatalogPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = (ROOT / "runtime/bootstrap.sh").read_text()
        cls.main = (ROOT / "main.tf").read_text()
        cls.unit = cls.bootstrap.split(
            "cat > /etc/systemd/system/ai-gateway-model-catalog.service <<'UNIT'\n", 1,
        )[1].split("\nUNIT\n", 1)[0]
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(cls.unit)
        cls.service = parser["Service"]
        cls.readiness = cls.bootstrap.split(
            "cat > /usr/local/lib/ai-gateway/model-catalog-ready.py <<'PY'\n", 1,
        )[1].split("\nPY\n", 1)[0]

    def test_catalog_bundle_and_root_owned_install_without_new_dependencies(self):
        assets = dict(re.findall(
            r'^\s+"([^"]+)"\s*=\s*file\("\$\{path.module\}/runtime/([^"]+)"\)',
            self.main, re.M,
        ))
        names = {"bootstrap.sh", "fetch-secrets.py", "config.json", "Caddyfile", "backup.sh",
                 "iap-auth.py", "iap-auth.service", "model-catalog.py"}
        self.assertEqual(assets, {name: name for name in names})
        self.assertIn("install -d -o root -g root -m 0750 /opt/ai-gateway", self.main)
        self.assertIn("'${base64encode(content)}' | base64 --decode", self.main)
        self.assertIn("[[ $EUID -eq 0 ]]", self.bootstrap)
        for command in (
            "install -d -o root -g root -m 0755 /usr/local/lib/ai-gateway",
            "install -o root -g root -m 0644 /opt/ai-gateway/model-catalog.py /usr/local/lib/ai-gateway/model-catalog.py",
            "chown root:root /usr/local/lib/ai-gateway/model-catalog-ready.py",
            "chmod 0644 /usr/local/lib/ai-gateway/model-catalog-ready.py",
            "chown root:root /etc/systemd/system/ai-gateway-model-catalog.service",
            "chmod 0644 /etc/systemd/system/ai-gateway-model-catalog.service",
        ):
            self.assertIn(command, self.bootstrap)
        self.assertEqual(re.findall(r"apt-get install -y ([^\n]+)", self.bootstrap),
                         ["docker.io docker-compose-v2 python3 sqlite3"])
        self.assertNotRegex(self.bootstrap, r"\b(pip|pip3|uv|curl|wget)\b")
        self.assertNotRegex(self.bootstrap, r"(?m)^set .*([-+]x|xtrace)")

    def test_catalog_unit_is_nonroot_isolated_and_cannot_read_gateway_state(self):
        # Mirror the verifier baseline, with stricter file/network restrictions.
        baseline = configparser.ConfigParser(interpolation=None)
        baseline.optionxform = str
        baseline.read(ROOT / "runtime/iap-auth.service")
        for name in (
            "Restart", "RestartSec", "UMask", "NoNewPrivileges", "PrivateTmp",
            "PrivateDevices", "ProtectSystem", "ProtectHome", "ProtectKernelTunables",
            "ProtectKernelModules", "ProtectControlGroups", "RestrictSUIDSGID", "LockPersonality",
            "CapabilityBoundingSet", "MemoryMax", "TasksMax", "StandardOutput", "StandardError",
        ):
            self.assertEqual(self.service[name], baseline["Service"][name], name)
        for name, value in {
            "ExecStart": "/usr/bin/python3 -I /usr/local/lib/ai-gateway/model-catalog.py",
            "ExecStartPost": "/usr/bin/python3 -I /usr/local/lib/ai-gateway/model-catalog-ready.py",
            "RestrictAddressFamilies": "AF_UNIX AF_INET",
            "User": "ai-gateway-catalog",
            "Group": "ai-gateway-catalog",
            "IPAddressDeny": "any",
            "IPAddressAllow": "127.0.0.1/32",
            "SocketBindDeny": "any",
            "SocketBindAllow": "ipv4:tcp:9092",
            "LimitCORE": "0",
            "TimeoutStartSec": "30",
        }.items():
            self.assertEqual(self.service[name], value, name)
        self.assertEqual(set(self.service["InaccessiblePaths"].split()), {
            "/opt/ai-gateway", "/srv/ai-gateway", "/run/ai-gateway",
        })
        for name in self.service:
            self.assertFalse(name.startswith(("Environment", "LoadCredential", "SetCredential")))
        for name in ("DynamicUser", "SupplementaryGroups", "ReadWritePaths", "BindPaths", "PrivateNetwork"):
            self.assertNotIn(name, self.service)
        self.assertIn("useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ai-gateway-catalog", self.bootstrap)
        self.assertIn('0 < account.pw_uid < 1000', self.bootstrap)
        self.assertIn("install -d -o root -g root -m 0700 /run/ai-gateway", self.bootstrap)

    def test_catalog_uses_fixed_loopback_addresses_not_public_firewall(self):
        module = ast.parse((ROOT / "runtime/model-catalog.py").read_text())
        addresses = {
            target.id: ast.literal_eval(node.value)
            for node in module.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and target.id in ("LISTEN_ADDRESS", "UPSTREAM_ADDRESS")
        }
        self.assertEqual(addresses, {
            "LISTEN_ADDRESS": ("127.0.0.1", 9092), "UPSTREAM_ADDRESS": ("127.0.0.1", 8080),
        })
        firewall = self.main.split('resource "google_compute_firewall" "public_web" {', 1)[1]
        firewall = firewall.split('\nresource "', 1)[0]
        self.assertEqual(re.findall(r"ports\s*=\s*\[([^]]+)\]", firewall), ['"80", "443"'])
        self.assertNotIn("9092", firewall)

    def test_caddy_start_waits_for_catalog_readiness_on_boot_and_rerun(self):
        gateway = self.bootstrap.split("cat > /etc/systemd/system/ai-gateway.service <<'UNIT'\n", 1)[1]
        gateway = gateway.split("\nUNIT\n", 1)[0]
        for directive in ("Wants", "After"):
            values = " ".join(re.findall(r"(?m)^" + directive + "=(.*)$", gateway)).split()
            self.assertIn("ai-gateway-model-catalog.service", values)
        self.assertNotIn("Requires=ai-gateway-model-catalog.service", gateway)
        self.assertNotIn("ai-gateway.service", self.unit)
        self.assertNotIn("docker.service", self.unit)
        commands = (
            "install -o root -g root -m 0644 /opt/ai-gateway/model-catalog.py",
            "systemctl daemon-reload",
            "systemctl enable ai-gateway-model-catalog.service",
            "systemctl restart ai-gateway-model-catalog.service",
            "systemctl restart ai-gateway.service",
        )
        positions = [self.bootstrap.index(command) for command in commands]
        self.assertEqual(positions, sorted(positions))

    def test_readiness_retries_without_credentials_or_upstream_access(self):
        with mock.patch("http.client.HTTPConnection") as connect, mock.patch("time.sleep") as sleep:
            connection = connect.return_value
            connection.request.side_effect = [ConnectionRefusedError(), http.client.BadStatusLine(""), None]
            connection.getresponse.return_value.status = 401
            exec(compile(self.readiness, "model-catalog-ready.py", "exec"), {})
            self.assertEqual(connect.call_args_list,
                             [mock.call("127.0.0.1", 9092, timeout=1)] * 3)
            self.assertEqual(connection.request.call_args_list, [mock.call("GET", "/v1/models")] * 3)
            self.assertEqual(connection.close.call_count, 3)
            self.assertEqual(sleep.call_args_list, [mock.call(0.25)] * 2)

    def test_readiness_fails_closed_with_bounded_retries(self):
        for status in (200, 403, 404, 502, None):
            with self.subTest(status=status), mock.patch("http.client.HTTPConnection") as connect, \
                    mock.patch("time.sleep") as sleep:
                connection = connect.return_value
                connection.getresponse.return_value.status = status
                if status is None:
                    connection.request.side_effect = ConnectionRefusedError()
                with self.assertRaises(SystemExit) as raised:
                    exec(compile(self.readiness, "model-catalog-ready.py", "exec"), {})
                self.assertEqual(raised.exception.code, 1)
                self.assertEqual(connect.call_count, 20)
                self.assertEqual(connection.close.call_count, 20)
                self.assertEqual(sleep.call_count, 20)