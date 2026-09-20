"""Isolated runtime tests: no cloud, Docker, mount, apt, or bootstrap execution."""

import ast
import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SPEC = importlib.util.spec_from_file_location("gateway_secrets", RUNTIME / "fetch-secrets.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def credentials():
    # Synthetic validation data only; never use a live secret in these tests.
    return {
        "BIFROST_ADMIN_USERNAME": "test-admin",
        "BIFROST_ADMIN_PASSWORD": "p" * 32,
        "BIFROST_ENCRYPTION_KEY": "e" * 32,
        "BIFROST_SETUP_TOKEN": "s" * 32,
    }


def deployment():
    return {
        "project_id": "test-project",
        "bootstrap_secret_id": "ai-gateway-bootstrap-json",
        "bootstrap_secret_version": "latest",
        "data_device": "/dev/disk/by-id/google-ai-gateway-data",
        "data_mount": "/srv/ai-gateway",
    }


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def network(payload=None, identity=None):
    if payload is None:
        payload = json.dumps(credentials()).encode()
    if identity is None:
        identity = {"access_token": "synthetic-test-identity", "token_type": "Bearer"}
    opener = mock.Mock()
    opener.open.side_effect = [
        response(identity),
        response({"payload": {"data": base64.b64encode(payload).decode("ascii")}}),
    ]
    return opener


class CredentialValidationTests(unittest.TestCase):
    def test_exact_allowlist_and_safe_characters(self):
        values = {key: "Az09_./+=:@-" * 3 for key in helper.KEYS}
        self.assertEqual(helper.validate_credentials(values), values)
        self.assertEqual(set(helper.KEYS), set(credentials()))

    def test_missing_unexpected_keys_and_non_objects(self):
        invalid = [None, [], "not-an-object", {}]
        for key in helper.KEYS:
            values = credentials()
            del values[key]
            invalid.append(values)
        invalid.append(dict(credentials(), EXTRA="not-allowed"))
        for values in invalid:
            with self.subTest(kind=type(values).__name__), self.assertRaises(ValueError):
                helper.validate_credentials(values)

    def test_all_fields_reject_injection_and_non_string_values(self):
        suffixes = [
            "\n", "\r", "\r\nOTHER=value", "\x00", " ", "\t", "#comment",
            "$USER", "${USER}", "$(id)", "`id`", "'", '"', "\\", ";id",
            "\u2028", "\u00e9", "%", "*",
        ]
        for key in helper.KEYS:
            for index, value in enumerate(["a" * 32 + suffix for suffix in suffixes] + [None, 42, True, []]):
                with self.subTest(key=key, case=index), self.assertRaises(ValueError):
                    helper.validate_credentials(dict(credentials(), **{key: value}))

    def test_length_boundaries(self):
        for size in (1, 128):
            helper.validate_credentials(dict(credentials(), BIFROST_ADMIN_USERNAME="a" * size))
        for size in (0, 129):
            with self.assertRaises(ValueError):
                helper.validate_credentials(dict(credentials(), BIFROST_ADMIN_USERNAME="a" * size))
        for key in helper.KEYS[1:]:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    helper.validate_credentials(dict(credentials(), **{key: "a" * 31}))
                helper.validate_credentials(dict(credentials(), **{key: "a" * 32}))

    def test_duplicate_keys_are_rejected(self):
        payload = json.dumps(credentials())[:-1] + ',"BIFROST_ADMIN_USERNAME":"other"}'
        with self.assertRaises(ValueError):
            helper.fetch_credentials(deployment(), network(payload.encode()))


class CredentialNetworkTests(unittest.TestCase):
    def test_metadata_then_latest_secret_with_private_headers(self):
        opener = network()
        self.assertEqual(helper.fetch_credentials(deployment(), opener), credentials())
        self.assertEqual(opener.open.call_count, 2)
        first, second = opener.open.call_args_list
        metadata_request = first.args[0]
        self.assertEqual(metadata_request.full_url, helper.METADATA_URL)
        self.assertEqual(dict(metadata_request.header_items()), {"Metadata-flavor": "Google"})
        secret_request = second.args[0]
        self.assertEqual(secret_request.full_url, (
            "https://secretmanager.googleapis.com/v1/projects/test-project/"
            "secrets/ai-gateway-bootstrap-json/versions/latest:access"
        ))
        self.assertEqual(dict(secret_request.header_items()), {"Authorization": "Bearer synthetic-test-identity"})
        for call in (first, second):
            self.assertEqual(call.kwargs, {"timeout": 20})
            self.assertIsNone(call.args[0].data)

    def test_default_client_disables_proxies_and_redirects(self):
        with mock.patch.object(helper.urllib.request, "build_opener", return_value=network()) as build:
            helper.fetch_credentials(deployment())
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, helper.NoRedirect)
        self.assertIsNone(redirect.redirect_request(None, None, 302, None, None, "https://invalid.example"))

    def test_invalid_identity_never_calls_secret_manager(self):
        for identity in (
            {"access_token": "synthetic\r\nInjected: header", "token_type": "Bearer"},
            {"access_token": "", "token_type": "Bearer"},
            {"access_token": 42, "token_type": "Bearer"},
            {"access_token": "synthetic", "token_type": "Other"},
        ):
            opener = network(identity=identity)
            with self.assertRaises(ValueError):
                helper.fetch_credentials(deployment(), opener)
            self.assertEqual(opener.open.call_count, 1)

    def test_malformed_secret_envelopes_and_payloads(self):
        for envelope in ({}, {"payload": {"data": "%%%"}}, {"payload": {"data": 42}}):
            opener = network()
            opener.open.side_effect = [
                response({"access_token": "synthetic", "token_type": "Bearer"}), response(envelope),
            ]
            with self.assertRaises((KeyError, ValueError, TypeError)):
                helper.fetch_credentials(deployment(), opener)
        for payload in (b"not-json", b"\xff", b"[]", b"{}"):
            with self.assertRaises(ValueError):
                helper.fetch_credentials(deployment(), network(payload))

    def test_response_size_is_bounded(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b"x" * (helper.MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(ValueError):
            helper.fetch_credentials(deployment(), opener)


class CredentialFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "deployment.json"
        self.config.write_text(json.dumps(deployment()), encoding="utf-8")
        self.env = self.root / "run" / "bootstrap.env"

    def test_deployment_contract_and_identifier_validation(self):
        self.assertEqual(helper.load_deployment(self.config), deployment())
        changes = [
            {"project_id": "test-project/other"}, {"bootstrap_secret_id": "../other"},
            {"bootstrap_secret_id": "bad?query"}, {"bootstrap_secret_version": "1"},
            {"data_device": "/dev/sda"}, {"data_mount": "/"},
            {"data_device": None}, {"extra": "not-allowed"},
        ]
        for change in changes:
            self.config.write_text(json.dumps(dict(deployment(), **change)), encoding="utf-8")
            with self.assertRaises(ValueError):
                helper.load_deployment(self.config)
        missing = deployment()
        del missing["data_mount"]
        self.config.write_text(json.dumps(missing), encoding="utf-8")
        with self.assertRaises(ValueError):
            helper.load_deployment(self.config)

    def test_atomic_private_write_under_permissive_umask(self):
        self.env.parent.mkdir(mode=0o755)
        self.env.write_text("previous-env", encoding="ascii")
        original_replace = os.replace

        def check_replace(source, destination):
            self.assertEqual(self.env.read_text(), "previous-env")
            self.assertEqual(Path(source).parent, self.env.parent)
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.env.parent.stat().st_mode), 0o700)
            original_replace(source, destination)

        old_umask = os.umask(0)
        try:
            with mock.patch.object(helper.os, "replace", side_effect=check_replace) as replace:
                helper.write_env(credentials(), self.env)
            replace.assert_called_once()
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)
        self.assertEqual(self.env.stat().st_uid, os.geteuid())
        self.assertEqual(self.env.read_text(), "".join(key + "=" + credentials()[key] + "\n" for key in helper.KEYS))
        self.assertEqual(list(self.env.parent.iterdir()), [self.env])

    def test_filesystem_failure_preserves_previous_env_and_removes_temporary(self):
        helper.write_env(credentials(), self.env)
        before = self.env.read_bytes()
        for operation in ("fsync", "replace"):
            with mock.patch.object(helper.os, operation, side_effect=OSError("synthetic-error")):
                with self.assertRaises(OSError):
                    helper.write_env(dict(credentials(), BIFROST_ADMIN_USERNAME="new-admin"), self.env)
            self.assertEqual(self.env.read_bytes(), before)
            self.assertEqual(list(self.env.parent.iterdir()), [self.env])

    def test_rejects_environment_and_directory_symlinks(self):
        self.env.parent.mkdir()
        target = self.root / "untouched"
        target.write_text("previous", encoding="ascii")
        self.env.symlink_to(target)
        with self.assertRaises(ValueError):
            helper.write_env(credentials(), self.env)
        self.assertEqual(target.read_text(), "previous")
        linked_directory = self.root / "linked-run"
        linked_directory.symlink_to(self.env.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            helper.write_env(credentials(), linked_directory / "other.env")

    def test_invalid_credentials_do_not_create_files(self):
        with self.assertRaises(ValueError):
            helper.write_env(dict(credentials(), EXTRA="not-allowed"), self.env)
        self.assertFalse(self.env.parent.exists())

    def test_successful_entry_point_is_silent(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        actual_uid = os.geteuid()
        with (
            mock.patch.object(helper.os, "geteuid", side_effect=[0, actual_uid]),
            mock.patch.object(helper.urllib.request, "build_opener", return_value=network()),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            self.assertEqual(helper.main(self.config, self.env), 0)
        self.assertTrue(self.env.is_file())
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_failed_fetch_redacts_errors_and_preserves_old_environment(self):
        helper.write_env(credentials(), self.env)
        previous = self.env.read_bytes()
        errors = [
            urllib.error.HTTPError("https://invalid.example", 403, "synthetic-sensitive-error", {}, io.BytesIO(b"synthetic-payload")),
            urllib.error.URLError("synthetic-sensitive-error"),
            json.JSONDecodeError("synthetic-sensitive-error", "synthetic-payload", 0),
            ValueError("synthetic-sensitive-error"),
        ]
        for error in errors:
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(helper.os, "geteuid", return_value=0),
                mock.patch.object(helper, "fetch_credentials", side_effect=error),
                mock.patch.object(helper, "write_env") as write,
                redirect_stdout(stdout), redirect_stderr(stderr),
            ):
                self.assertEqual(helper.main(self.config, self.env), 1)
            write.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "Unable to prepare AI gateway credentials.\n")
            self.assertEqual(self.env.read_bytes(), previous)


class RuntimeSafetyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = (RUNTIME / "bootstrap.sh").read_text()
        cls.backup = (RUNTIME / "backup.sh").read_text()
        cls.prepare = cls.bootstrap.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    def test_shell_syntax_only_and_embedded_python_syntax(self):
        for filename in ("bootstrap.sh", "backup.sh"):
            with self.subTest(filename=filename):
                result = subprocess.run(["bash", "-n", str(RUNTIME / filename)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                content = (RUNTIME / filename).read_text()
                embedded = content.split("python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
                compile(embedded, filename, "exec")  # Compile only, never execute.

    def test_disk_format_requires_whole_nonboot_unsigned_disk(self):
        for guard in (
            'Path("/dev/disk/by-id/google-ai-gateway-data")',
            'Path("/srv/ai-gateway")', 'stat.S_ISBLK(expected.stat().st_mode)',
            'disks[0]["type"] == "disk"', 'boot_mounts = {"/", "/boot", "/boot/efi"}',
            'require(not disk.get("children"))',
            'require(not any(data.iterdir()))', 'require(not blank or not mounted)',
            'require(disk.get("fstype") == "ext4" and len(signatures or []) == 1)',
            'require(os.path.realpath(mounted[0]["source"]) == device)',
            'require(data.stat().st_dev == expected.stat().st_rdev)',
        ):
            self.assertIn(guard, self.prepare)
        tree = ast.parse(self.prepare)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run"]
        format_calls = [node for node in calls if isinstance(node.args[0], ast.Constant) and node.args[0].value == "mkfs.ext4"]
        self.assertEqual(len(format_calls), 1)
        self.assertEqual(len(format_calls[0].args), 2)  # No force option.
        self.assertIsInstance(format_calls[0].args[1], ast.Name)
        self.assertEqual(format_calls[0].args[1].id, "device")
        self.assertIn('if blank:\n        # No force flag', self.prepare)
        self.assertIn('run("wipefs", "--no-act", "--json", device))["signatures"]', self.prepare)
        self.assertLess(self.prepare.index('run("wipefs"'), self.prepare.index('run("mkfs.ext4"'))
        self.assertNotIn('"--force"', self.prepare)

    def test_fstab_and_config_are_idempotent_and_mount_precedes_data_writes(self):
        for text in (
            'require(len(at_target) <= 1)', 'entry[0] == "UUID=" + disk["uuid"]',
            'if not at_target:', 'os.replace(temporary, fstab)', 'ext4 defaults 0 2',
            'if not config.exists():', 'os.link(temporary, config)',
            'os.chown(config, 1000, 1000)', 'config.chmod(0o600)',
            '("bifrost", 1000, 1000, 0o750)', '("caddy/data", 0, 0, 0o700)',
            '("caddy/config", 0, 0, 0o700)', '("backups", 0, 0, 0o700)',
        ):
            self.assertIn(text, self.prepare)
        self.assertLess(self.prepare.index('run("mountpoint"'), self.prepare.index('for relative, uid, gid, mode'))
        self.assertLess(self.prepare.index('require(len(at_target) <= 1)'), self.prepare.index('run("mkfs.ext4"'))

    def test_systemd_gates_foreground_compose_on_mount_and_secret_fetch(self):
        for text in (
            'apt-get install -y docker.io docker-compose-v2 python3 sqlite3',
            'Requires=docker.service', 'After=network-online.target docker.service',
            'RequiresMountsFor=/srv/ai-gateway', 'Type=simple',
            'ExecStartPre=/usr/bin/mountpoint --quiet /srv/ai-gateway',
            'ExecStartPre=/usr/bin/python3 /opt/ai-gateway/fetch-secrets.py',
            'RuntimeDirectoryMode=0700', 'StartLimitIntervalSec=0',
            'Restart=always', 'RestartSec=30', 'systemctl daemon-reload',
            'systemctl restart ai-gateway.service',
        ):
            self.assertIn(text, self.bootstrap)
        start = next(line for line in self.bootstrap.splitlines() if line.startswith("ExecStart=/usr/bin/docker"))
        stop = next(line for line in self.bootstrap.splitlines() if line.startswith("ExecStop="))
        self.assertTrue(start.endswith(" up --remove-orphans --abort-on-container-exit"))
        self.assertTrue(stop.endswith(" down"))
        self.assertNotIn("--volumes", stop)
        for forbidden in ("curl ", "wget ", "docker inspect", "compose config", "export BIFROST", "source /run/"):
            self.assertNotIn(forbidden, self.bootstrap)

    def test_backups_use_online_sqlite_atomic_completion_and_scoped_retention(self):
        for text in (
            'OnCalendar=*-*-* 03:00:00 UTC', 'Persistent=true',
            'RequiresMountsFor=/srv/ai-gateway',
        ):
            self.assertIn(text, self.bootstrap)
        self.assertRegex((RUNTIME.parent / "main.tf").read_text(), r'start_time\s*=\s*"04:00"')
        for text in (
            '["sqlite3", "-readonly", "-cmd", ".timeout 30000"', '".backup \'"',
            'tempfile.mkdtemp(prefix=".pending-"', 'os.replace(temporary, backups / ("sqlite-" + timestamp))',
            'destination.chmod(0o600)', 'cutoff = now - timedelta(days=14)',
            'old.is_symlink()', 'if created < cutoff:', 'shutil.rmtree(old)',
            'data.stat().st_dev != expected.stat().st_rdev',
        ):
            self.assertIn(text, self.backup)
        self.assertLess(self.backup.index('os.replace(temporary'), self.backup.index('cutoff ='))
        self.assertNotIn("bootstrap.env", self.backup)
        self.assertNotIn("rm -", self.backup)
        self.assertNotIn("shutil.copy", self.backup)


if __name__ == "__main__":
    unittest.main()