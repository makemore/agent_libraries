"""Offline checks for checkout safety; no application dependencies or services."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "workspace", Path(__file__).resolve().parents[1] / "scripts/workspace.py"
)
workspace = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workspace)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workspace-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "coordinator"
        self.root.mkdir()
        self.git(self.root, "init", "-q")
        self.git(self.root, "config", "-f", ".gitmodules", "submodule.demo.path", "clients/demo")

    def git(self, root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def test_manifest_is_inventory(self):
        self.assertEqual(workspace.repositories(self.root), ["clients/demo"])

    def test_existing_gitfile_checkout_is_never_reset(self):
        target = self.root / "clients/demo"
        target.mkdir(parents=True)
        (target / ".git").write_text("gitdir: ../../.git/modules/demo\n")
        (target / "work.txt").write_text("uncommitted work\n")
        with patch.object(workspace, "repositories", return_value=["clients/demo"]), \
                patch.object(workspace.subprocess, "run") as run:
            workspace.checkout(self.root)
        run.assert_not_called()
        self.assertEqual((target / "work.txt").read_text(), "uncommitted work\n")

    def test_existing_nonrepository_is_not_overwritten(self):
        target = self.root / "clients/demo"
        target.mkdir(parents=True)
        (target / "work.txt").write_text("keep me")
        with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
            workspace.checkout(self.root)

    def test_unsafe_path_is_rejected(self):
        self.git(self.root, "config", "-f", ".gitmodules", "submodule.demo.path", "../outside")
        with self.assertRaises(ValueError):
            workspace.repositories(self.root)

    def test_missing_checkout_uses_pinned_commit_not_remote_tip(self):
        origin = Path(self.temp.name) / "origin"
        origin.mkdir()
        self.git(origin, "init", "-q")
        self.git(origin, "config", "user.name", "Workspace Test")
        self.git(origin, "config", "user.email", "workspace@example.invalid")
        self.git(origin, "commit", "-qm", "pinned", "--allow-empty")
        pinned = self.git(origin, "rev-parse", "HEAD")
        self.git(origin, "commit", "-qm", "newer", "--allow-empty")
        self.git(self.root, "config", "-f", ".gitmodules", "submodule.demo.url", str(origin))
        self.git(self.root, "add", ".gitmodules")
        self.git(self.root, "update-index", "--add", "--cacheinfo", f"160000,{pinned},clients/demo")
        # This fixture alone permits local-file Git transport; never network.
        with patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            workspace.checkout(self.root)
        target = self.root / "clients/demo"
        self.assertEqual(self.git(target, "rev-parse", "HEAD"), pinned)
        self.git(target, "switch", "-qc", "local-work")
        (target / "work.txt").write_text("keep me")
        workspace.checkout(self.root)
        self.assertEqual(self.git(target, "branch", "--show-current"), "local-work")
        self.assertEqual((target / "work.txt").read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()