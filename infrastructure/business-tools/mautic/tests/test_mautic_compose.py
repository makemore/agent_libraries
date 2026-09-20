from pathlib import Path
import sys
import unittest

# Shared contract checks stay inside the two app folders; no runtime dependency.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agentic-social" / "tests"))
from compose_support import ComposeContract


class MauticComposeTests(ComposeContract, unittest.TestCase):
    folder = Path(__file__).resolve().parents[1]
    app = "mautic"
    domain = "marketing"
    images = {"mautic", "mariadb"}
    services = {"mautic", "mautic-cron", "mautic-worker", "mautic-mariadb"}
    env_files = {"mautic.env", "mautic-db.env"}
    mounts = {f"/srv/business-tools/mautic/{name}" for name in
              ("config", "logs", "media/files", "media/images", "mariadb")}

    def test_official_roles_and_shared_configuration(self):
        for name, role in (("mautic", "mautic_web"), ("mautic-cron", "mautic_cron"),
                           ("mautic-worker", "mautic_worker")):
            with self.subTest(service=name):
                service = self.service(name)
                self.assertIn("<<: *mautic-app", service)
                self.assertIn("<<: *mautic-environment", service)
                self.assertIn(f"DOCKER_MAUTIC_ROLE: {role}", service)
                self.assertIn("condition: service_healthy", service)
        self.assertIn('MAUTIC_SITE_URL: "https://${domains.marketing}"', self.content)
        self.assertIn('expose: ["80"]', self.service("mautic"))

    def test_sync_defaults_do_not_start_invalid_queue_consumers(self):
        self.assertIn("profiles: [mautic-workers]", self.service("mautic-worker"))
        self.assertNotIn("profiles:", self.service("mautic-cron"))
        for setting in ("MAUTIC_MESSENGER_DSN_", "DOCKER_MAUTIC_LOAD_TEST_DATA",
                        "DOCKER_MAUTIC_WORKERS_CONSUME_", "MAUTIC_COOKIE_SECURE",
                        "MAUTIC_TRUSTED_PROXIES", "MARIADB_AUTO_UPGRADE"):
            self.assertNotIn(setting, self.content)

    def test_exact_image_volume_targets_avoid_anonymous_children(self):
        for target in ("config", "var/logs", "docroot/media/files", "docroot/media/images"):
            self.assertEqual(self.content.count(f"target: /var/www/html/{target}\n"), 1)
        self.assertNotIn("target: /var/www/html/docroot/media\n", self.content)
        self.assertNotIn("target: /var/www/html\n", self.content)

    def test_private_database_and_outbound_worker_paths(self):
        for network in ("edge", "mautic-private", "mautic-proxy"):
            self.assertIn(network + ":", self.service("mautic"))
        self.assertIn("aliases: [mautic-web]", self.service("mautic"))
        self.assertIn("ipv4_address: 172.30.251.3", self.service("mautic"))
        self.assertIn("networks: [mautic-private]", self.service("mautic-mariadb"))
        for name in ("mautic-cron", "mautic-worker"):
            self.assertIn("networks: [mautic-private, mautic-egress]", self.service(name))
        self.assertEqual(self.content.count("internal: true"), 2)
        self.assertIn('"healthcheck.sh", "--connect", "--innodb_initialized"', self.content)

    def check_resolved(self, model):
        self.assertTrue(model["networks"]["mautic-private"]["internal"])
        self.assertTrue(model["networks"]["mautic-proxy"]["internal"])
        self.assertFalse(model["networks"]["mautic-egress"].get("internal", False))
        services = model["services"]
        self.assertEqual(services["mautic-worker"]["profiles"], ["mautic-workers"])
        for name in ("mautic-cron", "mautic-worker"):
            self.assertEqual(services[name]["volumes"], services["mautic"]["volumes"])
            self.assertNotIn("MAUTIC_MESSENGER_DSN_EMAIL", services[name]["environment"])
        self.assertEqual(len(services["mautic"]["volumes"]), 4)


if __name__ == "__main__":
    unittest.main()