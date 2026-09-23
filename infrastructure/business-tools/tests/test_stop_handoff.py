"""Isolated config-handoff tests; never contact systemd, Docker or the cloud.

Run from repository root: .venv/bin/python -B -m unittest discover -s infrastructure/business-tools/tests -p test_stop_handoff.py -v
Named exception: simulate Linux/root by remapping fixed paths to a temporary
filesystem and its UID/GID. Real modes, links, flock, fsync and atomic replacement
remain exercised. Synthetic helper source raises if executed, not if compiled.
The config-only copy exception is explicitly approved for the first-stop repair;
these tests do not claim a data backup or selected-image shutdown verification.
"""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install-stop-repair.py"
SPEC = importlib.util.spec_from_file_location("business_tools_stop_handoff", SCRIPT)
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)
HELPER_SOURCE = 'raise RuntimeError("synthetic helper must not execute")\n'
SENTINEL = "synthetic-private-diagnostic-must-not-escape"
EFFECTIVE = ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/business-tools/stop.py ; "
             "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")


class StopHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        uid, gid = os.geteuid(), os.getegid()
        paths = {
            "ROOT": self.root, "OWNER_UID": uid, "OWNER_GID": gid,
            "HELPER": self.root / "opt/business-tools/stop.py",
            "STAGED": self.root / "opt/business-tools/business-tools.service",
            "INSTALLED": self.root / "etc/systemd/system/business-tools.service",
            "LOCKS": (self.root / "run/business-tools-stage.lock",
                      self.root / "run/business-tools-maintenance.lock"),
            "BACKUP_ROOT": self.root / "var/tmp",
        }
        for name, value in paths.items():
            self.patch(handoff, name, value)
        for parent in (handoff.STAGED.parent, handoff.INSTALLED.parent,
                       handoff.LOCKS[0].parent, handoff.BACKUP_ROOT):
            parent.mkdir(parents=True, mode=0o700)
        handoff.BACKUP_ROOT.chmod(0o1777)
        for path in handoff.LOCKS:
            self.write(path, b"unchanged lock content", 0o600)
        source = (ROOT / "runtime/business-tools.service").read_text()
        self.old = source.replace(handoff.NEW, handoff.OLD).encode()
        self.assertEqual(self.old.count(handoff.OLD.encode()), 1)
        self.new = self.old.replace(handoff.OLD.encode(), handoff.NEW.encode())
        self.units(self.old)
        self.payload = {"helper": HELPER_SOURCE, "unit": self.new.decode()}
        self.properties = {"DropInPaths": "", "ActiveState": "active", "SubState": "running",
                           "MainPID": "321", "ExecStop": EFFECTIVE}
        self.after_reload = {}
        self.reloaded = False
        self.command = self.patch(handoff.subprocess, "run", side_effect=self.fake_systemctl)
        self.patch(handoff.sys, "platform", "linux")
        self.patch(handoff.os, "geteuid", return_value=0)
        self.real_flock = handoff.fcntl.flock
        self.lock_order = []
        self.patch(handoff.fcntl, "flock", side_effect=self.lock)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    @staticmethod
    def write(path, content, mode=0o600):
        path.write_bytes(content)
        path.chmod(mode)

    def units(self, content):
        self.write(handoff.STAGED, content)
        self.write(handoff.INSTALLED, content, 0o644)

    def installed(self):
        self.units(self.new)
        self.write(handoff.HELPER, HELPER_SOURCE.encode())

    def helper_revision(self):
        # Named opt-in exception: an explicitly reviewed previous helper, not a default.
        self.units(self.new)
        self.previous_helper = HELPER_SOURCE.encode() + b"# reviewed previous revision\n"
        self.write(handoff.HELPER, self.previous_helper, 0o644)
        return ("--previous-helper-sha256", hashlib.sha256(self.previous_helper).hexdigest())

    def lock(self, stream, flags):
        inode = os.fstat(stream.fileno()).st_ino
        self.lock_order.append(next(path for path in handoff.LOCKS if path.stat().st_ino == inode))
        self.assertEqual(flags, handoff.fcntl.LOCK_EX)
        if self.lock_order[-1] == handoff.LOCKS[1]:
            with handoff.LOCKS[0].open("rb") as stage:
                with self.assertRaises(BlockingIOError):
                    self.real_flock(stage, handoff.fcntl.LOCK_EX | handoff.fcntl.LOCK_NB)
        return self.real_flock(stream, flags)

    def released(self):
        for path in handoff.LOCKS:
            with path.open("rb") as stream:
                self.real_flock(stream, handoff.fcntl.LOCK_EX | handoff.fcntl.LOCK_NB)

    def fake_systemctl(self, args, **kwargs):
        if args == ("/usr/bin/systemctl", "daemon-reload"):
            self.reloaded = True
            value = ""
        else:
            name = args[2].removeprefix("--property=")
            value = self.after_reload.get(name, self.properties[name]) if self.reloaded else self.properties[name]
            if callable(value):
                value = value()
        return subprocess.CompletedProcess(args, 0, stdout=value + "\n")

    def invoke(self, argv=(), raw=None):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(handoff.sys, "stdin", io.StringIO(json.dumps(self.payload) if raw is None else raw)), \
                redirect_stdout(output), redirect_stderr(errors):
            code = handoff.main(list(argv))
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn(SENTINEL, output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        result = json.loads(output.getvalue())
        self.assertTrue(set(result) <= {"ok", "applied", "already_installed", "backup_dir"})
        for key in ("ok", "applied", "already_installed"):
            self.assertIs(type(result[key]), bool)
        self.assertEqual(code, 0 if result["ok"] else 1)
        for call in self.command.call_args_list:
            args, = call.args
            allowed = [("/usr/bin/systemctl", "daemon-reload")]
            allowed += [("/usr/bin/systemctl", "show", "--property=" + name, "--value", "business-tools.service")
                        for name in self.properties]
            self.assertIn(args, allowed)  # No stop/down/restart/reset-failed/backup or broad dump.
            self.assertEqual(call.kwargs, {
                "check": True, "text": True, "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "close_fds": True,
                "timeout": 30, "env": {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
            })
        return result

    def tree(self):
        result = {}
        for path in (self.root, *self.root.rglob("*")):
            info = path.lstat()
            content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[str(path.relative_to(self.root))] = (handoff.identity(info), content)
        return result

    def refused_unchanged(self, argv=("--apply",), raw=None):
        before = self.tree()
        result = self.invoke(argv, raw)
        self.assertEqual(result, {"ok": False, "applied": False, "already_installed": False})
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.reloaded)

    def assert_copies(self, result, old=None, old_helper=None):
        old = self.old if old is None else old
        copies = {"staged.service": old, "installed.service": old}
        if old_helper is not None:
            copies["stop.py"] = old_helper
        backup = Path(result["backup_dir"])
        self.assertEqual(backup.parent, handoff.BACKUP_ROOT)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual({path.name for path in backup.iterdir()}, set(copies) | {"hashes.json"})
        for path in backup.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
        for name, content in copies.items():
            self.assertEqual((backup / name).read_bytes(), content)
        self.assertEqual(json.loads((backup / "hashes.json").read_text()),
                         {name: hashlib.sha256(content).hexdigest() for name, content in copies.items()})

    def test_preview_has_no_writes_and_releases_ordered_locks_before_json(self):
        before = self.tree()
        def printed(*args, **kwargs):
            self.released()
            print(*args, **kwargs)
        with mock.patch.object(handoff, "print", side_effect=printed, create=True), \
                mock.patch.object(handoff.tempfile, "mkdtemp") as directory, \
                mock.patch.object(handoff.tempfile, "mkstemp") as temporary, \
                mock.patch.object(handoff.os, "replace") as replace, \
                mock.patch.object(handoff.os, "fsync") as fsync:
            self.assertEqual(self.invoke(), {"ok": True, "applied": False, "already_installed": False})
        for operation in (directory, temporary, replace, fsync):
            operation.assert_not_called()
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.lock_order, list(handoff.LOCKS))
        self.assertFalse(self.reloaded)

    def test_apply_copies_then_atomic_publication_and_one_reload_without_stop(self):
        with mock.patch.object(handoff.os, "replace", wraps=os.replace) as replace, \
                mock.patch.object(handoff.os, "fsync", wraps=os.fsync) as fsync:
            result = self.invoke(("--apply",))
        self.assertTrue(result["ok"] and result["applied"])
        self.assertFalse(result["already_installed"])
        self.assert_copies(result)
        backup = Path(result["backup_dir"])
        self.assertEqual([call.args[1] for call in replace.call_args_list],
                         [backup / "staged.service", backup / "installed.service", backup / "hashes.json",
                          handoff.HELPER, handoff.STAGED, handoff.INSTALLED])
        self.assertEqual(fsync.call_count, 13)  # Each file and parent, plus mkdtemp's parent.
        for path, content, mode in ((handoff.HELPER, HELPER_SOURCE.encode(), 0o600),
                                   (handoff.STAGED, self.new, 0o600), (handoff.INSTALLED, self.new, 0o644)):
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        reloads = [call for call in self.command.call_args_list if call.args[0][1] == "daemon-reload"]
        self.assertEqual(len(reloads), 1)
        self.assertEqual(self.lock_order, list(handoff.LOCKS))
        self.released()

    def test_matching_existing_helper_and_reviewed_runtime_source_compile_only(self):
        self.payload["helper"] = (ROOT / "runtime/stop.py").read_text()
        self.write(handoff.HELPER, self.payload["helper"].encode())
        self.assertTrue(self.invoke(("--apply",))["applied"])

    def test_idempotent_candidate_requires_helper_and_never_writes_or_reloads(self):
        self.installed()
        before = self.tree()
        with mock.patch.object(handoff.tempfile, "mkdtemp") as directory, \
                mock.patch.object(handoff.os, "replace") as replace:
            for argv in ((), ("--apply",)):
                self.assertEqual(self.invoke(argv), {"ok": True, "applied": False, "already_installed": True})
        directory.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.reloaded)

    def test_already_installed_without_helper_refused(self):
        self.units(self.new)
        self.refused_unchanged()

    def test_helper_revision_preview_has_no_writes_and_checks_effective_stop_and_pid(self):
        argv = self.helper_revision()
        before = self.tree()
        with mock.patch.object(handoff.tempfile, "mkdtemp") as directory, \
                mock.patch.object(handoff.tempfile, "mkstemp") as temporary, \
                mock.patch.object(handoff.os, "replace") as replace, \
                mock.patch.object(handoff.os, "fsync") as fsync:
            self.assertEqual(self.invoke(argv), {"ok": True, "applied": False, "already_installed": False})
        for operation in (directory, temporary, replace, fsync):
            operation.assert_not_called()
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.lock_order, list(handoff.LOCKS))
        properties = [call.args[0][2] for call in self.command.call_args_list]
        self.assertIn("--property=ExecStop", properties)
        self.assertEqual(properties.count("--property=MainPID"), 2)
        self.assertFalse(self.reloaded)
        self.released()

    def test_helper_revision_apply_backs_up_old_helper_and_never_rewrites_units(self):
        argv = self.helper_revision()
        annotated = b"# preserve installed annotation\n\n" + self.new.replace(b"Restart=always", b"  Restart=always  ")
        self.units(annotated)
        units = {path: handoff.snapshot(path) for path in (handoff.STAGED, handoff.INSTALLED)}
        real_replace = os.replace
        def replace(source, target):
            if target == handoff.HELPER:
                backup, = handoff.BACKUP_ROOT.iterdir()
                self.assert_copies({"backup_dir": str(backup)}, annotated, self.previous_helper)
            real_replace(source, target)
        with mock.patch.object(handoff.os, "replace", side_effect=replace) as replacement:
            result = self.invoke(("--apply", *argv))
        self.assertTrue(result["ok"] and result["applied"])
        self.assertFalse(result["already_installed"])
        self.assert_copies(result, annotated, self.previous_helper)
        backup = Path(result["backup_dir"])
        self.assertEqual([call.args[1] for call in replacement.call_args_list],
                         [backup / "staged.service", backup / "installed.service", backup / "stop.py",
                          backup / "hashes.json", handoff.HELPER])
        self.assertEqual({path: handoff.snapshot(path) for path in units}, units)
        self.assertEqual(handoff.HELPER.read_bytes(), HELPER_SOURCE.encode())
        self.assertEqual(stat.S_IMODE(handoff.HELPER.stat().st_mode), 0o600)
        properties = [call.args[0][2] for call in self.command.call_args_list]
        self.assertEqual(properties.count("--property=ExecStop"), 3)  # Preflight, pre/post replacement.
        self.assertEqual(properties.count("--property=MainPID"), 4)
        self.assertFalse(self.reloaded)
        self.assertEqual(self.lock_order, list(handoff.LOCKS))
        self.released()

    def test_helper_revision_retry_with_previous_hash_is_read_only_noop(self):
        argv = self.helper_revision()
        self.assertTrue(self.invoke(("--apply", *argv))["applied"])
        before = self.tree()
        with mock.patch.object(handoff.tempfile, "mkdtemp") as directory, \
                mock.patch.object(handoff.os, "replace") as replace:
            for flags in (argv, ("--apply", *argv), (), ("--apply",)):
                self.assertEqual(self.invoke(flags), {"ok": True, "applied": False, "already_installed": True})
        directory.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.reloaded)

    def test_helper_revision_requires_explicit_matching_hash_in_both_modes(self):
        self.helper_revision()
        for authorization in ((), ("--previous-helper-sha256", "0" * 64)):
            for mode in ((), ("--apply",)):
                with self.subTest(authorized=bool(authorization), apply=bool(mode)):
                    self.refused_unchanged((*mode, *authorization))

    def test_previous_helper_hash_requires_exact_lowercase_hex_even_for_noop(self):
        self.installed()
        invalid = ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "０" * 64,
                   "a" * 64 + "\n", " " + "a" * 64, SENTINEL)
        for value in invalid:
            with self.subTest(length=len(value)):
                self.refused_unchanged(("--apply", "--previous-helper-sha256", value))
        self.refused_unchanged(("--previous-helper-sha256",))
        self.refused_unchanged(("--previous-helper-sha", "a" * 64))
        self.command.assert_not_called()

    def test_previous_helper_hash_cannot_authorize_missing_helper_or_unit_changes(self):
        argv = self.helper_revision()
        for content in (self.new, self.old):
            self.units(content)
            for helper in (None, self.previous_helper, HELPER_SOURCE.encode()):
                if content == self.new and helper is not None:
                    continue
                if helper is None:
                    handoff.HELPER.unlink(missing_ok=True)
                else:
                    self.write(handoff.HELPER, helper)
                for mode in ((), ("--apply",)):
                    with self.subTest(candidate=content == self.new, missing=helper is None, apply=bool(mode)):
                        self.refused_unchanged((*mode, *argv))

    def test_helper_revision_refuses_wrong_unit_semantics_or_nonidentical_unit_bytes(self):
        argv = self.helper_revision()
        variants = (self.new.replace(b"Restart=always", b"Restart=no"),
                    self.new.replace(b"TimeoutStopSec=4200", b"TimeoutStopSec=5"),
                    self.new.replace(b"--abort-on-container-exit", b"--detach"),
                    self.new.replace(handoff.NEW.encode(), handoff.NEW.encode() + b"\nExecStop=/bin/true"))
        for unit in variants:
            self.units(unit)
            self.refused_unchanged(argv)
            self.refused_unchanged(("--apply", *argv))
        self.units(self.new)
        self.write(handoff.INSTALLED, self.new + b"\n# different bytes\n", 0o644)
        self.refused_unchanged(("--apply", *argv))
        self.units(self.new)
        self.payload["unit"] = variants[0].decode()  # Authorization must not grant a candidate unit change either.
        self.refused_unchanged(("--apply", *argv))

    def test_helper_revision_refuses_wrong_effective_stop_or_unstable_pid_before_writes(self):
        argv = self.helper_revision()
        self.properties["ExecStop"] = EFFECTIVE.replace("python3", "python3-other")
        for mode in ((), ("--apply",)):
            self.refused_unchanged((*mode, *argv))
        self.properties["ExecStop"] = EFFECTIVE
        for mode in ((), ("--apply",)):
            pids = iter(("321", "999"))
            self.properties["MainPID"] = lambda: next(pids)
            self.refused_unchanged((*mode, *argv))

    def test_helper_revision_hash_does_not_bypass_filesystem_guards(self):
        argv = ("--apply", *self.helper_revision())
        for path in (handoff.HELPER, handoff.STAGED, handoff.INSTALLED, *handoff.LOCKS):
            saved = path.with_name(path.name + ".saved")
            with self.subTest(target=path.name):
                path.rename(saved)
                try:
                    self.refused_unchanged(argv)  # No creation of missing helpers, units or locks.
                    path.symlink_to(saved)
                    self.refused_unchanged(argv)
                finally:
                    path.unlink(missing_ok=True)
                    saved.rename(path)
                os.link(path, saved)
                try:
                    self.refused_unchanged(argv)
                finally:
                    saved.unlink()
                mode = stat.S_IMODE(path.stat().st_mode)
                path.chmod(0o620)
                try:
                    self.refused_unchanged(argv)
                finally:
                    path.chmod(mode)
        real_lstat = Path.lstat
        for field in ("st_uid", "st_gid"):
            def changed_owner(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if path == handoff.HELPER:
                    values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                    values[field] += 1
                    return SimpleNamespace(**values)
                return info
            with self.subTest(field=field), mock.patch.object(Path, "lstat", changed_owner):
                self.refused_unchanged(argv)

    def test_helper_revision_change_during_preflight_is_preserved_without_backup(self):
        argv = self.helper_revision()
        changed = self.previous_helper + b"# concurrent edit\n"
        def change_helper():
            self.write(handoff.HELPER, changed)
            return "321"
        self.properties["MainPID"] = change_helper
        result = self.invoke(("--apply", *argv))
        self.assertFalse(result["ok"])
        self.assertNotIn("backup_dir", result)
        self.assertEqual(handoff.HELPER.read_bytes(), changed)
        self.assertFalse(self.reloaded)

    def test_helper_revision_change_during_atomic_write_is_not_overwritten(self):
        argv = self.helper_revision()
        changed = self.previous_helper + b"# concurrent edit during fsync\n"
        real_atomic, real_fsync = handoff.atomic, os.fsync
        def fsync(fd):
            real_fsync(fd)
            self.write(handoff.HELPER, changed)
        def atomic(path, *args):
            if path == handoff.HELPER:
                with mock.patch.object(handoff.os, "fsync", side_effect=fsync):
                    return real_atomic(path, *args)
            return real_atomic(path, *args)
        units = {path: handoff.snapshot(path) for path in (handoff.STAGED, handoff.INSTALLED)}
        with mock.patch.object(handoff, "atomic", side_effect=atomic):
            result = self.invoke(("--apply", *argv))
        self.assertFalse(result["ok"] or result["applied"])
        self.assert_copies(result, self.new, self.previous_helper)
        self.assertEqual(handoff.HELPER.read_bytes(), changed)
        self.assertEqual({path: handoff.snapshot(path) for path in units}, units)
        self.assertEqual(list(handoff.HELPER.parent.glob(".stop-repair-*")), [])
        self.assertFalse(self.reloaded)
        self.released()

    def test_helper_revision_rechecks_runtime_before_and_after_publication(self):
        real_replace = os.replace
        for phase in ("backup", "publication"):
            for key, value in (("MainPID", "999"), ("ExecStop", SENTINEL), ("DropInPaths", SENTINEL),
                               ("ActiveState", "failed"), ("SubState", "dead")):
                with self.subTest(phase=phase, property=key):
                    argv = self.helper_revision()
                    original = self.properties[key]
                    def replace(source, target):
                        real_replace(source, target)
                        if ((phase == "backup" and target.name == "hashes.json") or
                                (phase == "publication" and target == handoff.HELPER)):
                            self.properties[key] = value
                    with mock.patch.object(handoff.os, "replace", side_effect=replace):
                        result = self.invoke(("--apply", *argv))
                    self.properties[key] = original
                    self.assertFalse(result["ok"] or result["applied"])
                    self.assert_copies(result, self.new, self.previous_helper)
                    expected = self.previous_helper if phase == "backup" else HELPER_SOURCE.encode()
                    self.assertEqual(handoff.HELPER.read_bytes(), expected)  # Never roll back a publication.
                    self.assertFalse(self.reloaded)
                    self.released()

    def test_helper_revision_rechecks_units_before_and_after_publication_without_overwrite(self):
        real_replace = os.replace
        for phase in ("backup", "publication"):
            for path in (handoff.STAGED, handoff.INSTALLED):
                with self.subTest(phase=phase, target=path.name):
                    argv = self.helper_revision()
                    changed = self.new.replace(b"Restart=always", b"Restart=no")
                    def replace(source, target):
                        real_replace(source, target)
                        if ((phase == "backup" and target.name == "hashes.json") or
                                (phase == "publication" and target == handoff.HELPER)):
                            self.write(path, changed)
                    with mock.patch.object(handoff.os, "replace", side_effect=replace):
                        result = self.invoke(("--apply", *argv))
                    self.assertFalse(result["ok"] or result["applied"])
                    self.assert_copies(result, self.new, self.previous_helper)
                    self.assertEqual(path.read_bytes(), changed)
                    expected = self.previous_helper if phase == "backup" else HELPER_SOURCE.encode()
                    self.assertEqual(handoff.HELPER.read_bytes(), expected)
                    self.assertFalse(self.reloaded)

    def test_different_existing_helper_refused_in_both_modes(self):
        for content in (b"", HELPER_SOURCE.encode() + b"\n", SENTINEL.encode()):
            self.write(handoff.HELPER, content)
            for argv in ((), ("--apply",)):
                with self.subTest(length=len(content), apply=bool(argv)):
                    self.refused_unchanged(argv)

    def test_old_unit_semantic_mutations_are_refused(self):
        replacements = (("Restart=always", "Restart=no"), ("TimeoutStopSec=4200", "TimeoutStopSec=5"),
                        ("--abort-on-container-exit", "--detach"), (handoff.OLD, handoff.OLD + " --volumes"),
                        (handoff.OLD, handoff.OLD + "\nExecStop=/bin/true"),
                        ("[Service]", "[Service]\nEnvironment=NEW_OVERRIDE=1"))
        for old, new in replacements:
            with self.subTest(directive=old):
                changed = self.old.replace(old.encode(), new.encode())
                self.assertNotEqual(changed, self.old)
                self.units(changed)
                self.refused_unchanged()

    def test_blank_comments_and_outer_whitespace_are_ignored_for_delta(self):
        changed = b"# public annotation\n\n" + self.old.replace(b"Restart=always", b"  Restart=always  ")
        self.units(changed)
        result = self.invoke(("--apply",))
        self.assertTrue(result["applied"])
        self.assert_copies(result, changed)

    def test_staged_and_installed_must_match_bytes_not_only_semantics(self):
        self.write(handoff.INSTALLED, self.old + b"\n# public comment\n", 0o644)
        self.refused_unchanged()

    def test_candidate_must_have_only_fixed_execstop_in_service_section(self):
        source = self.new.decode()
        candidates = [source.replace(handoff.NEW, line) for line in
                      (handoff.OLD, handoff.NEW + " extra", handoff.NEW + "\nExecStop=",
                       handoff.NEW.replace("=/", "=-/"), handoff.NEW + "\\")]
        candidates += [source.replace("[Service]", "[Other]"), source + "\nRestart=no\n", source + "\0"]
        for candidate in candidates:
            with self.subTest(length=len(candidate)):
                self.payload["unit"] = candidate
                self.refused_unchanged()

    def test_missing_locks_or_units_fail_without_creating_anything(self):
        for path in (*handoff.LOCKS, handoff.STAGED, handoff.INSTALLED):
            with self.subTest(target=path.name):
                moved = path.with_name(path.name + ".saved")
                path.rename(moved)
                try:
                    self.refused_unchanged(())
                    self.refused_unchanged()
                finally:
                    moved.rename(path)

    def test_permissions_are_not_repaired(self):
        self.write(handoff.HELPER, HELPER_SOURCE.encode())
        for path in (*handoff.LOCKS, handoff.STAGED, handoff.INSTALLED, handoff.HELPER):
            old_mode = stat.S_IMODE(path.stat().st_mode)
            modes = (0o620, 0o602, 0o4600) + ((0o644,) if path in handoff.LOCKS else ())
            for mode in modes:
                with self.subTest(target=path.name, mode=mode):
                    path.chmod(mode)
                    try:
                        self.refused_unchanged()
                    finally:
                        path.chmod(old_mode)

    def test_unsafe_ancestor_or_nonsticky_backup_parent_refused(self):
        for path in (self.root, handoff.STAGED.parent, handoff.INSTALLED.parent,
                     handoff.LOCKS[0].parent, handoff.BACKUP_ROOT.parent, handoff.BACKUP_ROOT):
            old_mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o777 if path == handoff.BACKUP_ROOT else 0o770)
            try:
                with self.subTest(target=str(path.relative_to(self.root))):
                    self.refused_unchanged()
            finally:
                path.chmod(old_mode)

    def test_leaf_symlinks_and_hardlinks_refused_without_following(self):
        self.write(handoff.HELPER, HELPER_SOURCE.encode())
        for path in (*handoff.LOCKS, handoff.STAGED, handoff.INSTALLED, handoff.HELPER):
            saved = path.with_name(path.name + ".saved")
            for kind in ("symlink", "dangling", "hardlink"):
                with self.subTest(target=path.name, kind=kind):
                    if kind == "hardlink":
                        os.link(path, saved)
                    else:
                        path.rename(saved)
                        path.symlink_to(saved if kind == "symlink" else saved.with_suffix(".missing"))
                    try:
                        self.refused_unchanged()
                    finally:
                        path.unlink()
                        saved.rename(path)

    def test_symlink_ancestors_refused(self):
        for path in (handoff.STAGED.parent, handoff.INSTALLED.parent, handoff.LOCKS[0].parent, handoff.BACKUP_ROOT):
            moved = path.with_name(path.name + ".saved")
            path.rename(moved)
            path.symlink_to(moved, target_is_directory=True)
            try:
                with self.subTest(target=path.name):
                    self.refused_unchanged()
            finally:
                path.unlink()
                moved.rename(path)

    def test_nonregular_helper_refused_without_opening_fifo(self):
        for kind in ("directory", "fifo"):
            if kind == "directory":
                handoff.HELPER.mkdir(mode=0o700)
            else:
                os.mkfifo(handoff.HELPER, 0o600)
            try:
                self.refused_unchanged()
            finally:
                handoff.HELPER.rmdir() if kind == "directory" else handoff.HELPER.unlink()

    def test_wrong_numeric_owner_or_group_refused(self):
        real_lstat = Path.lstat
        for target in (handoff.STAGED, handoff.INSTALLED, handoff.LOCKS[0], handoff.STAGED.parent):
            for field in ("st_uid", "st_gid"):
                def changed_owner(path, *args, **kwargs):
                    info = real_lstat(path, *args, **kwargs)
                    if path == target:
                        values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                        values[field] += 1
                        return SimpleNamespace(**values)
                    return info
                with self.subTest(target=target.name, field=field), \
                        mock.patch.object(Path, "lstat", changed_owner):
                    self.refused_unchanged()

    def test_dropins_or_nonrunning_or_invalid_pid_refused(self):
        for key, value in (("DropInPaths", SENTINEL), ("ActiveState", "inactive"), ("SubState", "exited"),
                           *(('MainPID', value) for value in ("0", "-1", "one", "123\n456", "１２３"))):
            old = self.properties[key]
            self.properties[key] = value
            try:
                with self.subTest(property=key):
                    self.refused_unchanged()
            finally:
                self.properties[key] = old

    def test_effective_stop_accepts_only_single_exact_python_argv(self):
        self.installed()
        for value in ("", SENTINEL, EFFECTIVE + " " + EFFECTIVE,
                      EFFECTIVE.replace("stop.py ;", "stop.py extra ;"),
                      EFFECTIVE.replace("path=/usr/bin/python3", "path=/bin/sh"),
                      EFFECTIVE.replace("ignore_errors=no", "ignore_errors=yes"),
                      EFFECTIVE.replace("stop.py ;", "stop.py\n ;")):
            self.properties["ExecStop"] = value
            self.refused_unchanged()

    def test_pid_change_after_reload_fails_and_keeps_copies_without_rollback(self):
        self.after_reload["MainPID"] = "999"
        result = self.invoke(("--apply",))
        self.assertFalse(result["ok"] or result["applied"])
        self.assertTrue(self.reloaded)
        self.assert_copies(result)
        self.assertEqual(handoff.STAGED.read_bytes(), self.new)
        self.assertEqual(handoff.INSTALLED.read_bytes(), self.new)
        self.assertEqual(handoff.HELPER.read_bytes(), HELPER_SOURCE.encode())
        self.released()

    def test_after_reload_dropin_state_or_wrong_stop_fails_preserving_copies(self):
        for key, value in (("DropInPaths", SENTINEL), ("ActiveState", "failed"), ("SubState", "dead"),
                           ("ExecStop", EFFECTIVE.replace("python3", "python3-other"))):
            self.units(self.old)
            self.reloaded = False
            self.after_reload = {key: value}
            with self.subTest(property=key):
                result = self.invoke(("--apply",))
                self.assertFalse(result["ok"])
                self.assert_copies(result)
                self.assertEqual(handoff.INSTALLED.read_bytes(), self.new)

    def test_source_change_during_preflight_is_preserved_and_refused(self):
        changed = self.old + b"\n# concurrent source edit\n"
        def change_source():
            self.write(handoff.STAGED, changed)
            return "321"
        self.properties["MainPID"] = change_source
        result = self.invoke(("--apply",))
        self.assertFalse(result["ok"])
        self.assertNotIn("backup_dir", result)
        self.assertEqual(handoff.STAGED.read_bytes(), changed)
        self.assertEqual(handoff.INSTALLED.read_bytes(), self.old)
        self.assertFalse(handoff.HELPER.exists() or self.reloaded)

    def test_source_change_between_publications_is_not_overwritten(self):
        real_replace = os.replace
        changed = self.old + b"\n# concurrent source edit\n"
        def replace(source, target):
            real_replace(source, target)
            if target == handoff.HELPER:
                self.write(handoff.STAGED, changed)
        with mock.patch.object(handoff.os, "replace", side_effect=replace):
            result = self.invoke(("--apply",))
        self.assertFalse(result["ok"] or self.reloaded)
        self.assert_copies(result)
        self.assertEqual(handoff.STAGED.read_bytes(), changed)
        self.assertEqual(handoff.INSTALLED.read_bytes(), self.old)

    def test_target_change_after_reload_is_not_hidden(self):
        def change_source():
            self.write(handoff.INSTALLED, self.new + b"\n# concurrent source edit\n", 0o644)
            return "321"
        self.after_reload["MainPID"] = change_source
        result = self.invoke(("--apply",))
        self.assertFalse(result["ok"])
        self.assert_copies(result)
        self.assertNotEqual(handoff.INSTALLED.read_bytes(), self.new)

    def test_atomic_rechecks_target_after_tempfile_fsync(self):
        before = handoff.snapshot(handoff.STAGED)
        real_fsync = os.fsync
        changed = self.old + b"\n# changed during temporary write\n"
        def fsync(fd):
            real_fsync(fd)
            self.write(handoff.STAGED, changed)
        with mock.patch.object(handoff.os, "fsync", side_effect=fsync), \
                mock.patch.object(handoff.os, "replace") as replace, self.assertRaises(ValueError):
            handoff.atomic(handoff.STAGED, self.new, 0o600, before)
        replace.assert_not_called()
        self.assertEqual(handoff.STAGED.read_bytes(), changed)
        self.assertEqual(list(handoff.STAGED.parent.glob(".stop-repair-*")), [])

    def test_errors_and_invalid_cli_or_payload_never_echo_diagnostics(self):
        for argv in (("--help",), ("--ap",), ("--unknown=" + SENTINEL,), ("--apply=" + SENTINEL,)):
            self.refused_unchanged(argv)
        for raw in (SENTINEL, "[]", "null", json.dumps({"helper": SENTINEL}),
                    json.dumps({**self.payload, "unexpected": SENTINEL}),
                    json.dumps({"helper": 42, "unit": self.new.decode()})):
            self.refused_unchanged(raw=raw)
        for helper in ("", "def " + SENTINEL + "(:", 'value = "\\q"  # ' + SENTINEL):
            self.payload["helper"] = helper
            self.refused_unchanged()
        self.payload["helper"] = HELPER_SOURCE
        self.command.side_effect = subprocess.CalledProcessError(1, SENTINEL, output=SENTINEL, stderr=SENTINEL)
        self.refused_unchanged()
        self.released()

    def test_reload_exception_leaves_protected_copies_and_generic_json(self):
        def failure(args, **kwargs):
            if args[1] == "daemon-reload":
                raise OSError(SENTINEL)
            return self.fake_systemctl(args, **kwargs)
        self.command.side_effect = failure
        result = self.invoke(("--apply",))
        self.assertFalse(result["ok"])
        self.assert_copies(result)
        self.assertEqual(handoff.INSTALLED.read_bytes(), self.new)
        self.released()

    def test_cleanup_error_cannot_report_success_and_still_releases_locks(self):
        real_close = os.close
        parent_inode = handoff.BACKUP_ROOT.stat().st_ino
        def close(fd):
            is_backup_parent = os.fstat(fd).st_ino == parent_inode
            real_close(fd)
            if is_backup_parent:
                raise OSError(SENTINEL)
        with mock.patch.object(handoff.os, "close", side_effect=close):
            result = self.invoke(("--apply",))
        self.assertFalse(result["ok"] or result["applied"])
        self.assert_copies(result)
        self.released()

    def test_requires_root_linux(self):
        for module, name, value in ((handoff.sys, "platform", "darwin"), (handoff.os, "geteuid", lambda: 1000)):
            with mock.patch.object(module, name, value):
                self.refused_unchanged()
        self.command.assert_not_called()


if __name__ == "__main__":
    unittest.main()