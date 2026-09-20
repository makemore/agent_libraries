from pathlib import Path
import unittest

from compose_support import ComposeContract


class PostizComposeTests(ComposeContract, unittest.TestCase):
    folder = Path(__file__).resolve().parents[1]
    app = "postiz"
    domain = "social"
    images = {"postiz", "postgres", "redis", "temporal", "temporal_postgres"}
    services = {"postiz", "postiz-postgres", "postiz-redis", "postiz-temporal",
                "postiz-temporal-postgres"}
    env_files = {"postiz.env", "postiz-db.env", "temporal.env", "temporal-db.env"}
    mounts = {f"/srv/business-tools/postiz/{name}" for name in
              ("config", "uploads", "postgres", "redis", "temporal-postgres")}

    def test_urls_and_bundled_workers(self):
        web = self.service("postiz")
        self.assertIn('NEXT_PUBLIC_BACKEND_URL: "https://${domains.social}/api"', web)
        self.assertIn('BACKEND_INTERNAL_URL: "http://localhost:3000"', web)
        self.assertIn('expose: ["5000"]', web)
        self.assertIn('TEMPORAL_ADDRESS: "postiz-temporal:7233"', web)
        self.assertIn('RUN_CRON: "true"', web)
        self.assertIn("target: /config\n", web)
        self.assertIn("target: /uploads\n", web)
        self.assertEqual(web.count("condition: service_healthy"), 3)

    def test_single_account_security_policy_not_open_registration(self):
        self.assertIn('DISABLE_REGISTRATION: "true"', self.service("postiz"))
        for setting in ("NOT_SECURED:", "DISABLE_SSRF_PROTECTION:", "API_LIMIT:",
                        "STORAGE_PROVIDER:", "STRIPE_PUBLISHABLE_KEY:"):
            self.assertNotRegex(self.content, r"(?m)^\s*" + setting)

    def test_sql_visibility_and_namespace_startup_gate(self):
        temporal = self.service("postiz-temporal")
        self.assertIn("DB: postgres12", temporal)
        self.assertIn("POSTGRES_SEEDS: postiz-temporal-postgres", temporal)
        self.assertIn('BIND_ON_IP: "0.0.0.0"', temporal)
        self.assertIn("operator cluster health", temporal)
        self.assertIn("operator namespace describe", temporal)
        self.assertIn("--namespace default", temporal)
        for setting in ("ENABLE_ES:", "ES_SEEDS:", "DEFAULT_NAMESPACE_RETENTION:",
                        "SKIP_SCHEMA_SETUP:", "DYNAMIC_CONFIG_FILE_PATH:"):
            self.assertNotRegex(self.content, r"(?m)^\s*" + setting)

    def test_database_networks_have_no_egress(self):
        self.assertIn("networks: [edge, postiz-private]", self.service("postiz"))
        for name in ("postiz-postgres", "postiz-redis"):
            self.assertIn("networks: [postiz-private]", self.service(name))
        self.assertIn("networks: [postiz-temporal-db]",
                      self.service("postiz-temporal-postgres"))
        self.assertEqual(self.content.count("internal: true"), 2)

    def check_resolved(self, model):
        self.assertTrue(model["networks"]["postiz-private"]["internal"])
        self.assertTrue(model["networks"]["postiz-temporal-db"]["internal"])
        self.assertEqual(model["services"]["postiz"]["environment"]["DISABLE_REGISTRATION"], "true")
        self.assertEqual(len(model["services"]["postiz"]["volumes"]), 2)


if __name__ == "__main__":
    unittest.main()