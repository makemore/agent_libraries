"""Offline regression tests for shared fixture discovery and SSE replay."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class StubServerTests(unittest.TestCase):
    def setUp(self):
        self.app = server.create_app(no_delay=True)
        self.client = self.app.test_client()

    def create_run(self, fixture="simple_streaming", **body):
        response = self.client.post(
            "/api/agent-runtime/runs/", json=body,
            headers={"X-Test-Fixture": fixture},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def frames(self, run_id, route="stream"):
        response = self.client.get(f"/api/agent-runtime/runs/{run_id}/{route}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/event-stream")
        return [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                if line.startswith("data: ")]

    def test_fixtures_resolve_from_server_location_not_working_directory(self):
        expected = Path(__file__).resolve().parents[1] / "fixtures" / "sse"
        self.assertEqual(server.FIXTURES_DIR, expected)
        self.assertIn("simple_streaming", server.load_fixtures())

    def test_health_lists_shared_fixtures(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["ok"])
        self.assertIn("simple_streaming", response.json["fixtures"])

    def test_stream_and_events_routes_replay_same_run(self):
        run = self.create_run()
        for route in ("stream", "events"):
            frames = self.frames(run["id"], route)
            self.assertTrue(frames)
            self.assertTrue(all(frame["run_id"] == run["id"] for frame in frames))
            self.assertEqual(frames[-1]["type"], "run.succeeded")
            self.assertEqual([frame["seq"] for frame in frames], list(range(len(frames))))

    def test_duplicate_sequence_fixture_is_not_renumbered(self):
        run = self.create_run("duplicate_replayed_event")
        seqs = [frame["seq"] for frame in self.frames(run["id"])]
        self.assertEqual(seqs[:2], [0, 0])

    def test_unknown_fixture_returns_useful_error(self):
        response = self.client.post(
            "/api/agent-runtime/runs/", json={}, headers={"X-Test-Fixture": "missing"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unknown fixture", response.json["error"])

    def test_repeated_turns_get_unique_runs_and_keep_conversation(self):
        first = self.create_run()
        second = self.create_run(conversation_id=first["conversationId"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["conversationId"], second["conversationId"])

    def test_no_delay_mode_never_sleeps(self):
        run = self.create_run()
        with patch.object(server.time, "sleep") as sleep:
            self.frames(run["id"])
            sleep.assert_not_called()


class LauncherTests(unittest.TestCase):
    def test_launcher_works_outside_workspace_and_forwards_arguments(self):
        root = Path(__file__).resolve().parents[2]
        launcher = root / "clients/scripts/start_stub_server.sh"
        env = dict(os.environ, PYTHON=sys.executable, STUB_PORT="0")
        with tempfile.TemporaryDirectory(prefix="agent-stub-launcher-") as directory:
            result = subprocess.run(
                ["bash", str(launcher), "--help"], cwd=directory, env=env,
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-delay", result.stdout)
        self.assertIn(str(root / "test-harness/stub-server"), result.stdout)


if __name__ == "__main__":
    unittest.main()