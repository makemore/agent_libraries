"""Shared stdlib-only checks for the two application fragments, never live data.

Text checks are intentionally not a YAML parser. When installed, Compose performs
the real schema/merge validation with only synthetic inputs and empty env files.
This helper lives here because changes outside the two app folders are out of scope.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


class ComposeContract:
    """Mixin: subclasses provide folder, app, images, env_files and services."""

    @classmethod
    def setUpClass(cls):
        cls.template = (cls.folder / "compose.yaml.tftpl").read_text()
        cls.content = "\n".join(
            line for line in cls.template.splitlines()
            if not line.lstrip().startswith("#")
        )

    def service(self, name):
        services = self.content.split("\nservices:\n", 1)[-1]
        match = re.search(
            rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z]|^networks:|\Z)",
            services, re.M | re.S,
        )
        self.assertIsNotNone(match, name)
        return match[1]

    def test_template_inputs_are_only_public_maps(self):
        inputs = set(re.findall(r"\$\{([^}]+)\}", self.content))
        self.assertEqual(
            inputs,
            {f"images.{key}" for key in self.images} | {f"domains.{self.domain}"},
        )
        self.assertNotIn("%{", self.content)
        rendered = re.sub(r"\$\{[^}]+\}", "fixture", self.content)
        self.assertNotIn("$", rendered)

    def test_no_root_lifecycle_or_ingress_overrides(self):
        forbidden = (
            "ports", "network_mode", "container_name", "restart", "restart_policy",
            "external", "privileged", "user", "entrypoint", "command", "build",
            "use_api_socket", "volumes_from",
        )
        for key in forbidden:
            with self.subTest(key=key):
                self.assertNotRegex(self.content, rf"(?m)^\s*{key}:")
        self.assertNotIn("docker.sock", self.content)
        self.assertNotRegex(self.content, r"(?m)^volumes:")
        self.assertNotRegex(self.content, r"(?m)^  edge:")

    def test_names_and_secret_file_boundary(self):
        section = self.content.split("services:\n", 1)[1].split("\nnetworks:", 1)[0]
        self.assertEqual(set(re.findall(r"^  ([\w-]+):", section, re.M)), self.services)
        paths = re.findall(r"path: (/run/business-tools/[\w.-]+)", self.content)
        self.assertEqual(set(paths), {f"/run/business-tools/{f}" for f in self.env_files})
        self.assertEqual(self.content.count("format: raw"), len(paths))
        self.assertNotRegex(self.content, r"(?m)^\s+[A-Z_]*(?:PASSWORD|SECRET|PWD):")

    def test_all_mounts_are_fail_closed_binds(self):
        sources = re.findall(r"source: (\S+)", self.content)
        self.assertEqual(len(sources), len(self.mounts))
        self.assertEqual(set(sources), set(self.mounts))
        self.assertEqual(self.content.count("type: bind"), len(sources))
        self.assertEqual(self.content.count("create_host_path: false"), len(sources))
        self.assertNotRegex(self.content, r"(?m)^\s+- /[^\n]+:/")

    def test_compose_schema_and_resolved_mounts_when_available(self):
        if shutil.which("docker") is None:
            self.skipTest("Docker CLI unavailable; static checks still run")
        version = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if version.returncode:
            self.skipTest("Compose plugin unavailable; static checks still run")
        # No production env or .env is read: isolated cwd/project and explicit
        # empty CLI env file; every service env file is replaced before parsing.
        with tempfile.TemporaryDirectory(prefix="business-tools-contract-") as directory:
            root = Path(directory)
            empty = root / "empty.env"
            empty.write_text("")
            rendered = re.sub(
                r"\$\{(images|domains)\.([a-z_]+)\}",
                lambda m: (f"example.invalid/fixture/{m[2]}:test" if m[1] == "images"
                           else f"{m[2]}.example.invalid"),
                self.template,
            )
            for name in self.env_files:
                rendered = rendered.replace(f"/run/business-tools/{name}", str(empty))
            fragment = root / "app.yaml"
            fragment.write_text(rendered)
            base = root / "compose.yaml"
            base.write_text(json.dumps({"services": {}, "networks": {"edge": {"driver": "bridge"}}}))
            result = subprocess.run(
                ["docker", "compose", "--project-name", "business-tools-contract",
                 "--project-directory", str(root), "--env-file", str(empty),
                 "--profile", "*", "-f", str(base), "-f", str(fragment),
                 "config", "--format", "json"],
                cwd=root, capture_output=True, text=True, timeout=30, check=False,
            )
            # Never echo generic command output in assertion errors.
            self.assertEqual(result.returncode, 0, "Compose config validation failed")
            model = json.loads(result.stdout)
        self.assertEqual(set(model["services"]), self.services)
        for name, service in model["services"].items():
            with self.subTest(service=name):
                self.assertFalse(service.get("ports"))
                self.assertNotIn("restart", service)
                self.assertEqual("edge" in service["networks"], name == self.app)
                for mount in service.get("volumes", []):
                    self.assertEqual(mount["type"], "bind")
                    self.assertIn(mount["source"], self.mounts)
                    self.assertFalse(mount["bind"]["create_host_path"])
        self.check_resolved(model)
