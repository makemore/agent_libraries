"""Isolated stdlib tests; no PHP, service, cloud, mount or production config access.

Run: .venv/bin/python -B -m unittest discover -s infrastructure/business-tools/tests -p test_mautic_setup.py -v
Only numeric ownership is simulated to allow unprivileged temporary-directory
tests. Production's exact owner validator still runs, including rejection cases;
file types, modes, links, bytes and publication use the real temporary filesystem.
"""

from contextlib import redirect_stderr, redirect_stdout
import errno
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
SPEC = importlib.util.spec_from_file_location("business_tools_mautic_setup", RUNTIME / "prepare-mautic.py")
mautic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mautic)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # macOS /var is a symlink: resolve the fixture, not production inputs.
        self.root = Path(temporary.name).resolve()
        self.config = self.root / "config"
        self.config.mkdir(mode=0o700)
        self.target = self.config / mautic.FILENAME
        self.owners = {}
        owner_check = mautic.require_owner

        def simulated_owner(info):
            uid, gid = self.owners.get((info.st_dev, info.st_ino), (33, 33))
            owner_check(SimpleNamespace(st_uid=uid, st_gid=gid))

        # Named exception: only these unprivileged fixtures emulate UID/GID 33.
        # No production setting is changed; assert requested chown IDs on creation.
        self.patch(mautic, "require_owner", side_effect=simulated_owner)
        self.chown = self.patch(mautic.os, "fchown")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def seed(self):
        self.target.write_bytes(mautic.CONTENT)
        self.target.chmod(0o600)

    def identity(self, path):
        info = path.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns)

    def test_new_empty_config_publishes_exact_narrow_override(self):
        link = self.patch(mautic.os, "link", wraps=os.link)
        sync = self.patch(mautic.os, "fsync", wraps=os.fsync)
        mautic.prepare(self.config)
        self.assertEqual(self.target.read_bytes(),
                         b"<?php\n$parameters = ['trusted_proxies' => ['172.30.251.2']];\n")
        self.assertEqual(list(self.config.iterdir()), [self.target])
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(self.target.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
        self.assertEqual(self.chown.call_count, 1)
        self.assertEqual(self.chown.call_args.args[1:], (33, 33))
        self.assertEqual(link.call_count, 1)
        self.assertEqual(link.call_args.args[1], "parameters_local.php")
        self.assertFalse(link.call_args.kwargs["follow_symlinks"])
        self.assertEqual(sync.call_count, 2)

    def test_rerun_and_check_preserve_seed_and_later_local_php(self):
        mautic.prepare(self.config)
        local = self.config / "local.php"
        local.write_bytes(b"<?php /* isolated installed-config fixture */\n")
        local.chmod(0o600)
        before = {path: (self.identity(path), path.read_bytes()) for path in (self.target, local)}
        directory = self.identity(self.config)
        with mock.patch.object(mautic.os, "link") as link, \
                mock.patch.object(mautic.os, "fsync") as sync:
            mautic.prepare(self.config)
            mautic.prepare(self.config, check_only=True)
        link.assert_not_called()
        sync.assert_not_called()
        self.assertEqual(self.chown.call_count, 1)
        self.assertEqual(self.identity(self.config), directory)
        for path, state in before.items():
            self.assertEqual((self.identity(path), path.read_bytes()), state)

    def test_custom_override_preserved_and_rejected_without_parsing_php(self):
        for content in (b"", b"<?php /* operator-owned custom settings */\n",
                        mautic.CONTENT + b"\n", b"\xff" * 4096):
            with self.subTest(content_length=len(content)):
                self.target.write_bytes(content)
                self.target.chmod(0o600)
                before = self.identity(self.target)
                for check_only in (False, True):
                    with self.assertRaisesRegex(ValueError, "manual review required"):
                        mautic.prepare(self.config, check_only=check_only)
                self.assertEqual(self.identity(self.target), before)
                self.assertEqual(self.target.read_bytes(), content)
                self.assertEqual(list(self.config.iterdir()), [self.target])
        self.chown.assert_not_called()

    def test_any_existing_config_entry_without_override_blocks_creation(self):
        for name in ("local.php", "other.php", ".operator-backup", "subdirectory", "dangling"):
            with self.subTest(name=name):
                entry = self.config / name
                if name == "subdirectory":
                    entry.mkdir()
                elif name == "dangling":
                    entry.symlink_to(self.root / "missing")
                else:
                    entry.write_bytes(b"isolated operator-owned fixture")
                before = self.identity(entry)
                with self.assertRaises(ValueError):
                    mautic.prepare(self.config)
                self.assertEqual(self.identity(entry), before)
                self.assertFalse(self.target.exists())
                self.assertEqual(list(self.config.iterdir()), [entry])
                entry.rmdir() if name == "subdirectory" else entry.unlink()
        self.chown.assert_not_called()

    def test_check_empty_config_fails_without_writes(self):
        before = self.identity(self.config)
        with mock.patch.object(mautic.os, "link") as link, \
                mock.patch.object(mautic.os, "fsync") as sync:
            with self.assertRaises(ValueError):
                mautic.prepare(self.config, check_only=True)
        self.assertEqual(self.identity(self.config), before)
        self.assertEqual(list(self.config.iterdir()), [])
        self.chown.assert_not_called()
        link.assert_not_called()
        sync.assert_not_called()

    def test_missing_config_is_not_created(self):
        self.config.rmdir()
        for check_only in (False, True):
            with self.assertRaises(OSError):
                mautic.prepare(self.config, check_only=check_only)
        self.assertFalse(self.config.exists())

    def test_symlink_config_and_ancestor_are_rejected(self):
        alias = self.root / "alias"
        for destination, path in ((self.config, alias), (self.root, alias / "config")):
            with self.subTest(path=path):
                alias.symlink_to(destination, target_is_directory=True)
                with self.assertRaises(OSError):
                    mautic.prepare(path)
                self.assertEqual(list(self.config.iterdir()), [])
                alias.unlink()

    def test_non_directory_ancestor_is_rejected(self):
        regular = self.root / "regular"
        regular.write_bytes(b"isolated fixture")
        with self.assertRaises(OSError):
            mautic.prepare(regular / "config")
        self.assertEqual(regular.read_bytes(), b"isolated fixture")

    def test_relative_and_parent_traversal_paths_are_rejected(self):
        for path in (Path("config"), self.config / ".." / "config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                mautic.prepare(path)

    def test_target_symlink_even_dangling_is_rejected(self):
        destination = self.root / "operator.php"
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    destination.write_bytes(mautic.CONTENT)
                    destination.chmod(0o600)
                self.target.symlink_to(destination)
                with self.assertRaises(ValueError):
                    mautic.prepare(self.config)
                self.assertTrue(self.target.is_symlink())
                self.assertEqual(destination.exists(), existing)
                if existing:
                    self.assertEqual(destination.read_bytes(), mautic.CONTENT)
                self.target.unlink()

    def test_hardlinked_target_is_rejected_and_unchanged(self):
        self.seed()
        other = self.root / "operator.php"
        os.link(self.target, other)
        before = self.identity(self.target)
        with self.assertRaises(ValueError):
            mautic.prepare(self.config)
        self.assertEqual(self.identity(self.target), before)
        self.assertEqual(other.read_bytes(), mautic.CONTENT)

    def test_directory_and_fifo_target_are_rejected_before_open(self):
        self.target.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            mautic.prepare(self.config)
        self.target.rmdir()
        os.mkfifo(self.target, 0o600)
        with self.assertRaises(ValueError):
            mautic.prepare(self.config)
        self.assertTrue(stat.S_ISFIFO(self.target.lstat().st_mode))

    def test_unsafe_modes_are_not_repaired(self):
        self.seed()
        for path, mode in ((self.config, 0o750), (self.config, 0o1700),
                           (self.target, 0o644), (self.target, 0o400), (self.target, 0o1600)):
            with self.subTest(path=path.name, mode=mode):
                original = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode)
                before = self.identity(path)
                with self.assertRaises(ValueError):
                    mautic.prepare(self.config)
                self.assertEqual(self.identity(path), before)
                path.chmod(original)
        self.chown.assert_not_called()

    def test_wrong_directory_or_file_owners_are_not_repaired(self):
        self.seed()
        for path in (self.config, self.target):
            info = path.stat()
            for ids in ((0, 33), (33, 0), (1000, 1000)):
                with self.subTest(path=path.name, ids=ids):
                    self.owners[(info.st_dev, info.st_ino)] = ids
                    with self.assertRaises(ValueError):
                        mautic.prepare(self.config)
            self.owners.clear()
        self.chown.assert_not_called()
        self.assertEqual(self.target.read_bytes(), mautic.CONTENT)

    def test_publication_race_never_overwrites_existing_target(self):
        real_link = os.link
        operator = b"operator setting appeared during publication"

        def competing_link(*args, **kwargs):
            self.target.write_bytes(operator)
            return real_link(*args, **kwargs)

        with mock.patch.object(mautic.os, "link", side_effect=competing_link):
            with self.assertRaises(FileExistsError):
                mautic.prepare(self.config)
        self.assertEqual(self.target.read_bytes(), operator)
        self.assertEqual(list(self.config.iterdir()), [self.target])

    def test_config_arrival_before_publication_cleans_temporary(self):
        real_fsync = os.fsync
        local = self.config / "local.php"

        def config_arrival(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                local.write_bytes(b"operator-created installed config")
            return real_fsync(descriptor)

        with mock.patch.object(mautic.os, "fsync", side_effect=config_arrival):
            with self.assertRaises(ValueError):
                mautic.prepare(self.config)
        self.assertEqual(list(self.config.iterdir()), [local])
        self.assertEqual(local.read_bytes(), b"operator-created installed config")

    def test_prepublication_failures_remove_only_temporary_file(self):
        for operation in ("fchown", "fchmod", "fsync", "link"):
            with self.subTest(operation=operation):
                with mock.patch.object(mautic.os, operation, side_effect=OSError(errno.EIO, "fixture")):
                    with self.assertRaises(OSError):
                        mautic.prepare(self.config)
                self.assertEqual(list(self.config.iterdir()), [])


class ContractTests(unittest.TestCase):
    def test_default_path_and_exact_identity(self):
        self.assertEqual(mautic.CONFIG, Path("/srv/business-tools/mautic/config"))
        mautic.require_owner(SimpleNamespace(st_uid=33, st_gid=33))
        for uid, gid in ((0, 33), (33, 0), (1000, 1000)):
            with self.subTest(uid=uid, gid=gid), self.assertRaises(ValueError):
                mautic.require_owner(SimpleNamespace(st_uid=uid, st_gid=gid))

    def test_directory_fsync_unsupported_only_is_tolerated(self):
        for number in (errno.EINVAL, errno.ENOTSUP):
            with mock.patch.object(mautic.os, "fsync", side_effect=OSError(number, "fixture")):
                mautic.sync_directory(123)
        with mock.patch.object(mautic.os, "fsync", side_effect=OSError(errno.EIO, "fixture")):
            with self.assertRaises(OSError):
                mautic.sync_directory(123)

    def test_main_root_guard_does_not_prepare(self):
        stderr = io.StringIO()
        with mock.patch.object(mautic.os, "geteuid", return_value=1000), \
                mock.patch.object(mautic, "prepare") as prepare, redirect_stderr(stderr):
            self.assertEqual(mautic.main([]), 1)
        prepare.assert_not_called()
        self.assertEqual(stderr.getvalue(), mautic.ERROR + "\n")

    def test_main_check_and_normal_mode_dispatch_without_output(self):
        for args, check_only in (([], False), (["--check"], True)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(args=args), mock.patch.object(mautic.os, "geteuid", return_value=0), \
                    mock.patch.object(mautic, "prepare") as prepare, \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(mautic.main(args), 0)
            prepare.assert_called_once_with(check_only=check_only)
            self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_main_failures_print_only_fixed_manual_review_message(self):
        for error in (ValueError("synthetic private config content"),
                      OSError("synthetic private exception details")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(error_type=type(error).__name__), \
                    mock.patch.object(mautic.os, "geteuid", return_value=0), \
                    mock.patch.object(mautic, "prepare", side_effect=error), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(mautic.main([]), 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), mautic.ERROR + "\n")


if __name__ == "__main__":
    unittest.main()