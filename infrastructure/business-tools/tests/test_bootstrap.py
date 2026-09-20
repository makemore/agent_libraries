"""Isolated stdlib tests: no cloud, Docker daemon, mount, mkfs or host writes.

Run: python3 -m unittest discover -s infrastructure/business-tools/tests -p test_bootstrap.py -v
Numeric ownership is mocked ONLY for these unprivileged temporary-directory tests.
Production defaults and all filesystem type/mode/symlink checks remain exercised.
"""

import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
SPEC = importlib.util.spec_from_file_location("business_tools_disk", RUNTIME / "prepare-disk.py")
disk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(disk)
UUID = "12345678-1234-1234-1234-123456789abc"
DEVICE = "/dev/test-business-tools-data"


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o755)
        self.data = self.root / "data"
        self.fstab = self.root / "fstab"
        self.fstab.write_text("# Preserve operator entries\n/dev/test-boot / ext4 defaults 0 1\n")
        self.fstab.chmod(0o644)
        self.expected = self.root / "by-id"
        self.deployment = {"data_device": str(self.expected), "data_mount": str(self.data)}
        self.write_deployment()
        self.block = {"name": DEVICE, "type": "disk", "fstype": None, "uuid": None,
                      "mountpoints": [None], "ro": False}
        self.signatures = []
        self.mounted = False
        self.other_mounts = []
        self.mount_override = {}
        self.uuid_devices = [DEVICE]

        def patch(name, **kwargs):
            patcher = mock.patch.object(disk, name, **kwargs)
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value

        patch("DATA_DEVICE", new=self.expected)
        patch("DATA_MOUNT", new=self.data)
        patch("FSTAB", new=self.fstab)
        patch("SOURCE_ROOT", new=self.root)
        patch("RUNTIME_ROOT", new=self.root / "env")
        patch("device_identity", return_value=(DEVICE, self.root.stat().st_dev))
        # Local exception: unprivileged tests cannot chown to app/root UIDs.
        # Keep every actual path/type/mode check; verify the requested IDs below.
        patch("root_owned", side_effect=self.check_test_file)
        self.run = patch("run", side_effect=self.command)
        chown = mock.patch.object(disk.os, "chown")
        self.chown = chown.start()
        self.addCleanup(chown.stop)
        fchown = mock.patch.object(disk.os, "fchown")
        fchown.start()
        self.addCleanup(fchown.stop)

    def write_deployment(self):
        path = self.root / "deployment.json"
        path.write_text(json.dumps(self.deployment))
        path.chmod(0o600)

    @staticmethod
    def check_test_file(path, *, directory=False, mode=None):
        disk.no_symlinks(path)
        info = path.lstat()
        disk.require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        disk.require(not info.st_mode & 0o022)
        if not directory:
            disk.require(info.st_nlink == 1)
        if mode is not None:
            disk.require(stat.S_IMODE(info.st_mode) == mode)
        return info

    def command(self, *args):
        if args[0] == "lsblk":
            return json.dumps({"blockdevices": [self.block]})
        if args[0] == "findmnt":
            mounts = copy.deepcopy(self.other_mounts)
            if self.mounted:
                mounts.append({"target": str(self.data), "source": DEVICE, "fstype": "ext4",
                               "uuid": UUID, "options": "rw,relatime", **self.mount_override})
            return json.dumps({"filesystems": mounts})
        if args[0] == "wipefs":
            self.assertEqual(args, ("wipefs", "--no-act", "--json", DEVICE))
            return json.dumps({"signatures": self.signatures})
        if args[0] == "mkfs.ext4":
            self.assertEqual(args, ("mkfs.ext4", DEVICE))
            self.existing_ext4()
            return ""
        if args[:3] == ("blkid", "-s", "TYPE"):
            return self.block["fstype"]
        if args[:3] == ("blkid", "-s", "UUID"):
            return UUID
        if args[:2] == ("blkid", "-t"):
            return "\n".join(self.uuid_devices)
        if args == ("mount", str(self.data)):
            self.mounted = True
            self.block["mountpoints"] = [str(self.data)]
            return ""
        if args == ("mountpoint", "--quiet", str(self.data)):
            self.assertTrue(self.mounted)
            return ""
        if args == ("findfs", "LABEL=business-tools"):
            return DEVICE
        raise AssertionError("Unexpected mocked command: " + args[0])

    def existing_ext4(self):
        self.block.update(fstype="ext4", uuid=UUID)
        self.signatures = [{"type": "ext4"}]

    def refused(self, **kwargs):
        with self.assertRaises((ValueError, FileNotFoundError)):
            disk.prepare(**kwargs)
        self.assertNotIn("mkfs.ext4", [call.args[0] for call in self.run.call_args_list])
        self.assertNotIn("mount", [call.args[0] for call in self.run.call_args_list])

    def test_blank_disk_formats_exact_device_once_and_persists_uuid(self):
        disk.prepare()
        self.assertIn(mock.call("mkfs.ext4", DEVICE), self.run.call_args_list)
        contents = self.fstab.read_text()
        self.assertIn("# Preserve operator entries", contents)
        self.assertIn(f"UUID={UUID} {self.data} ext4 defaults 0 2\n", contents)
        self.assertEqual(stat.S_IMODE(self.fstab.stat().st_mode), 0o644)
        self.run.reset_mock()
        self.chown.reset_mock()
        disk.prepare()
        self.assertEqual(self.fstab.read_text(), contents)
        self.assertNotIn("mkfs.ext4", [call.args[0] for call in self.run.call_args_list])
        self.chown.assert_not_called()

    def test_existing_unmounted_ext4_is_mounted_without_formatting(self):
        self.existing_ext4()
        disk.prepare()
        self.assertIn(mock.call("mount", str(self.data)), self.run.call_args_list)
        self.assertNotIn("mkfs.ext4", [call.args[0] for call in self.run.call_args_list])

    def test_read_only_gate_never_formats_a_blank_disk(self):
        self.refused(check_only=True)

    def test_existing_tree_ownership_contents_and_modes_are_preserved(self):
        disk.prepare()
        mysql = self.data / "invoice-ninja/mysql"
        mysql.chmod(0o750)  # Named existing-owner scenario: upstream has initialized it.
        marker = mysql / "operator-data"
        marker.write_text("preserve")
        self.chown.reset_mock()
        disk.prepare()
        self.chown.assert_not_called()
        self.assertEqual(stat.S_IMODE(mysql.stat().st_mode), 0o750)
        self.assertEqual(marker.read_text(), "preserve")
        disk.prepare(check_only=True)

    def test_all_new_leaf_owners_and_parent_traversal(self):
        disk.prepare()
        specs = {relative: (uid, gid, mode) for relative, uid, gid, mode in disk.DIRECTORIES}
        for relative, (uid, gid, mode) in specs.items():
            with self.subTest(relative=relative):
                path = self.data / relative
                self.assertIn(mock.call(path, uid, gid, follow_symlinks=False), self.chown.call_args_list)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                for parent in path.parents:
                    if parent == self.data.parent:
                        break
                    self.assertTrue(parent.stat().st_mode & 0o001)
        self.assertEqual(specs["actual-budget/data"], (1001, 1001, 0o700))
        self.assertEqual(specs["observability/grafana"], (472, 0, 0o700))
        self.assertEqual(specs["observability/prometheus"], (65534, 65534, 0o700))
        self.assertEqual(specs["mautic/mariadb"], (0, 0, 0o700))
        self.assertEqual(specs["invoice-ninja/mysql"], (0, 0, 0o700))

    def test_wrong_deployment_paths_refused(self):
        for key in ("data_device", "data_mount"):
            with self.subTest(key=key):
                original = self.deployment[key]
                self.deployment[key] = "/wrong"
                self.write_deployment()
                self.refused()
                self.deployment[key] = original

    def test_partition_boot_and_readonly_devices_refused(self):
        variants = [
            {"type": "part"}, {"ro": True}, {"name": "/dev/wrong"},
            {"mountpoints": ["/"]}, {"mountpoints": ["/boot"]},
            {"mountpoints": ["/boot/efi"]}, {"mountpoints": ["/elsewhere"]},
            {"children": [{"name": DEVICE + "1", "mountpoints": [None]}]},
            {"children": [{"name": DEVICE + "1", "mountpoints": ["/"]}]},
        ]
        original = self.block.copy()
        for variant in variants:
            with self.subTest(variant=variant):
                self.block = {**original, **variant}
                self.refused()

    def test_signature_and_filesystem_conflicts_refused(self):
        for fstype, signatures in (
            (None, [{"type": "gpt"}]), (None, [{"type": "LVM2_member"}]),
            ("xfs", [{"type": "xfs"}]), ("ext4", []),
            ("ext4", [{"type": "ext4"}, {"type": "dos"}]),
        ):
            with self.subTest(fstype=fstype, signatures=signatures):
                self.block.update(fstype=fstype, uuid=UUID if fstype else None)
                self.signatures = signatures
                self.refused()

    def test_nonempty_unmounted_mountpoint_refused(self):
        self.data.mkdir()
        (self.data / "user-file").write_text("preserve")
        self.refused()
        self.assertEqual((self.data / "user-file").read_text(), "preserve")

    def test_mountpoint_and_fstab_symlinks_refused(self):
        self.data.symlink_to(self.root, target_is_directory=True)
        self.refused()
        self.data.unlink()
        self.fstab.unlink()
        self.fstab.symlink_to(self.root / "missing")
        self.refused()

    def test_wrong_source_nested_mount_and_readonly_mount_refused(self):
        self.existing_ext4()
        self.mounted = True
        for values in ({"source": "/dev/wrong"}, {"source": DEVICE + "[/subdir]"},
                       {"fstype": "xfs"}, {"options": "ro,relatime"},
                       {"target": str(self.data / "child")}):
            with self.subTest(values=values):
                self.mount_override = values
                self.refused()

    def test_boot_mount_inventory_cannot_hide_behind_missing_lsblk_mounts(self):
        self.other_mounts = [{"target": "/", "source": DEVICE, "fstype": "ext4", "uuid": UUID}]
        self.refused()

    def test_fstab_conflicts_refused_before_format(self):
        cases = [
            f"/dev/wrong {self.data} ext4 defaults 0 2\n",
            f"{DEVICE} /elsewhere ext4 defaults 0 2\n",
            f"LABEL=business-tools /elsewhere ext4 defaults 0 2\n",
            f"/dev/wrong {self.data}/child ext4 defaults 0 2\n",
            f"{self.data} /elsewhere none bind 0 0\n",
        ]
        for contents in cases:
            with self.subTest(contents=contents):
                self.fstab.write_text(contents)
                self.refused()

    def test_fstab_duplicate_uuid_and_unsafe_options_refused(self):
        self.existing_ext4()
        line = f"UUID={UUID} {self.data} ext4 defaults 0 2\n"
        for contents in (line * 2, line.replace("defaults", "nofail"),
                         line.replace("defaults", "ro"), line.replace("defaults", "bind"),
                         line.replace("defaults", "noauto"), line.replace("ext4", "xfs"),
                         line.replace(UUID, "87654321-4321-4321-4321-cba987654321")):
            with self.subTest(contents=contents):
                self.fstab.write_text(contents)
                self.refused()

    def test_duplicate_uuid_on_another_disk_refused(self):
        self.existing_ext4()
        self.uuid_devices.append("/dev/wrong")
        self.refused()

    def test_nested_data_symlink_refused_without_touching_target(self):
        disk.prepare()
        media = self.data / "mautic/media/files"
        media.rmdir()
        target = self.root / "unrelated"
        target.mkdir(mode=0o700)
        media.symlink_to(target, target_is_directory=True)
        self.chown.reset_mock()
        with self.assertRaises(ValueError):
            disk.prepare()
        self.chown.assert_not_called()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_existing_nontraversable_parent_is_refused_not_chmodded(self):
        disk.prepare()
        parent = self.data / "actual-budget"
        parent.chmod(0o700)
        self.chown.reset_mock()
        with self.assertRaises(ValueError):
            disk.prepare()
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)
        self.chown.assert_not_called()

    def test_backup_gate_requires_every_key_file_and_rejects_links(self):
        disk.prepare()
        disk.RUNTIME_ROOT.mkdir(mode=0o700)
        for name in disk.ENV_FILES:
            (disk.RUNTIME_ROOT / name).touch(mode=0o600)
        disk.check_backup()
        key = disk.RUNTIME_ROOT / "invoice-ninja.env"
        key.unlink()
        with self.assertRaises(FileNotFoundError):
            disk.check_backup()
        key.symlink_to(self.root / "deployment.json")
        with self.assertRaises(ValueError):
            disk.check_backup()


class PureSafetyTests(unittest.TestCase):
    def test_defaults_are_exact_gce_paths(self):
        self.assertEqual(str(disk.DATA_DEVICE), "/dev/disk/by-id/google-business-tools-data")
        self.assertEqual(str(disk.DATA_MOUNT), "/srv/business-tools")
        self.assertEqual(str(disk.SOURCE_ROOT), "/opt/business-tools")

    def test_device_must_be_a_symlink_to_a_block_device(self):
        path = mock.Mock()
        path.is_symlink.return_value = False
        with self.assertRaises(ValueError):
            disk.device_identity(path)
        path.is_symlink.return_value = True
        path.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG, st_rdev=123)
        with self.assertRaises(ValueError):
            disk.device_identity(path)
        path.stat.return_value = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=123)
        path.resolve.return_value = Path(DEVICE)
        self.assertEqual(disk.device_identity(path), (DEVICE, 123))

    def test_root_owned_config_rejects_wrong_owner_write_bits_and_hardlinks(self):
        for changes in ({"st_uid": 1000}, {"st_gid": 1000},
                        {"st_mode": stat.S_IFREG | 0o666}, {"st_nlink": 2}):
            path = mock.Mock()
            path.lstat.return_value = SimpleNamespace(
                **({"st_uid": 0, "st_gid": 0, "st_mode": stat.S_IFREG | 0o600,
                    "st_nlink": 1} | changes))
            with mock.patch.object(disk, "no_symlinks"), self.assertRaises(ValueError):
                disk.root_owned(path)

    def test_fstab_octal_escapes_are_parsed_not_ignored(self):
        self.assertEqual(disk.parse_fstab(r"/dev/test /srv/business\040tools ext4 defaults 0 2")[0][1],
                         "/srv/business tools")

    def test_cli_error_never_prints_exception_contents(self):
        output = io.StringIO()
        with mock.patch.object(disk.os, "geteuid", return_value=0), \
             mock.patch.object(disk, "prepare", side_effect=ValueError("private diagnostic")), \
             mock.patch.object(disk.sys, "argv", ["prepare-disk.py"]), \
             mock.patch.object(disk.sys, "stderr", output):
            self.assertEqual(disk.main(), 1)
        self.assertNotIn("private diagnostic", output.getvalue())


@unittest.skipUnless(shutil.which("bash"), "bash is required for syntax/mock-script checks")
class ScriptTests(unittest.TestCase):
    @staticmethod
    def bootstrap_functions():
        source = (RUNTIME / "bootstrap.sh").read_text()
        # Only function declarations; NEVER execute bootstrap's package/system work.
        source, separator, _ = source.partition('if [[ ${1:-} == --metadata-firewall')
        if not separator or "apt-get" in source or "exec 9>" in source:
            raise AssertionError("Bootstrap function boundary changed; refusing script execution")
        return source.replace("[[ $EUID -eq 0 ]]", "[[ 1 -eq 1 ]]")

    def test_shell_syntax_without_execution(self):
        for name in ("bootstrap.sh", "compose.sh", "backup.sh"):
            with self.subTest(name=name):
                result = subprocess.run(["bash", "-n", str(RUNTIME / name)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_compose_version_gate_keeps_raw_env_contract(self):
        for version, accepted in (("2.30.0", True), ("v2.40.1", True),
                                  ("2.30.3+ds1-0ubuntu1", True), ("3.0.0", True),
                                  ("2.29.9", False), ("1.99.0", False),
                                  ("2.30.0-rc1", False), ("unparseable", False)):
            with self.subTest(version=version):
                source = ('docker() { printf "%s\\n" "$TEST_VERSION"; }\n' +
                          self.bootstrap_functions() + "\nrequire_compose_version\n")
                result = subprocess.run(["bash", "-c", source], capture_output=True, text=True,
                                        env={"PATH": os.defpath, "TEST_VERSION": version})
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if not accepted:
                    self.assertIn("Docker Compose >= 2.30.0", result.stderr)

    def test_metadata_firewall_is_idempotent_with_mocked_iptables(self):
        mocks = r'''
installed_docker=0
installed_bridge=0
iptables() {
    case "$2" in
        -nL) return 0 ;;
        --check)
            if [[ $5 == docker0 ]]; then [[ $installed_docker == 1 ]]; else [[ $installed_bridge == 1 ]]; fi ;;
        --insert)
            if [[ $6 == docker0 ]]; then installed_docker=1; else installed_bridge=1; fi
            printf '%s\n' inserted ;;
        *) return 1 ;;
    esac
}
'''
        # The production function suppresses iptables output, so verify mock state.
        source = mocks + self.bootstrap_functions() + """
metadata_firewall
[[ $installed_docker == 1 && $installed_bridge == 1 ]]
iptables() { [[ $2 == -nL || $2 == --check ]]; }
metadata_firewall
"""
        result = subprocess.run(["bash", "-c", source], capture_output=True, text=True,
                                env={"PATH": os.defpath})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compose_file_order_and_foreground_systemd_contract(self):
        wrapper = (RUNTIME / "compose.sh").read_text()
        self.assertEqual(re.findall(r"--file (\S+)", wrapper), [
            "/opt/business-tools/compose.yaml",
            *(f"/opt/business-tools/{app}/compose.yaml" for app in disk.APPS),
        ])
        self.assertIn('exec /usr/bin/docker compose --project-name business-tools', wrapper)
        self.assertTrue(wrapper.rstrip().endswith('"$@"'))
        unit = (RUNTIME / "business-tools.service").read_text()
        for required in ("Type=simple", "--abort-on-container-exit", "Restart=always", "RestartSec=30",
                         "StandardOutput=null", "StandardError=null", "RuntimeDirectoryPreserve=yes",
                         "fetch-secrets.py", "RequiresMountsFor=/srv/business-tools"):
            self.assertIn(required, unit)
        self.assertGreaterEqual(int(re.search(r"TimeoutStopSec=(\d+)", unit)[1]), 3700)
        self.assertNotIn("--force-recreate", unit)
        self.assertNotIn("--volumes", unit)
        self.assertNotIn("maintenance.lock", unit)

    def test_metadata_rule_precedes_docker_return_and_does_not_block_host(self):
        source = (RUNTIME / "bootstrap.sh").read_text()
        self.assertIn("--check DOCKER-USER", source)
        self.assertIn("--insert DOCKER-USER 1", source)
        self.assertIn("--destination 169.254.169.254/32 --jump DROP", source)
        commands = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(commands, r"(?:--insert|--append|--policy) (?:OUTPUT|INPUT|FORWARD)")
        self.assertIn("docker.io docker-compose-v2 python3 logrotate iptables", source)
        self.assertIn("--debug /opt/business-tools/invoice-ninja/logrotate.conf", source)

    def test_backup_policy_is_narrow_and_includes_runtime_keys(self):
        source = (RUNTIME / "backup.sh").read_text()
        self.assertIn("--exclude='./srv/business-tools/backups'", source)
        self.assertIn("./srv/business-tools ./run/business-tools", source)
        self.assertIn("-mindepth 1 -maxdepth 1", source)
        self.assertIn("-mmin +20160 -delete", source)
        self.assertIn("-type f -uid 0 -gid 0 -links 1", source)
        self.assertIn("Persistent=false", (RUNTIME / "business-tools-backup.timer").read_text())

    def run_mock_backup(self, failure=""):
        # Named failure-mode harness: only a temporary copy runs; every system,
        # archive and retention command is a shell mock. Real writes are confined
        # to this TemporaryDirectory, with no daemon, root or mount access needed.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "backups").mkdir()
            source = (RUNTIME / "backup.sh").read_text()
            source = source.replace("[[ $EUID -eq 0 ]]", "[[ 1 -eq 1 ]]")
            source = source.replace("/run/business-tools-", str(root / "lock-"))
            source = source.replace("/srv/business-tools/backups", str(root / "backups"))
            self.assertNotIn("/run/business-tools-", source)
            self.assertNotIn("/srv/business-tools/backups", source)
            mocks = r'''
record() { printf '%s\n' "$*" >> "$EVENT_LOG"; }
python3() { record safety-check; }
flock() { record "flock $*"; }
systemctl() {
    record "systemctl $*"
    case "$1" in
        stop) [[ $FAIL_STAGE != stop ]] || return 1; : > "$TEST_ROOT/stopped" ;;
        start) [[ $FAIL_STAGE != start ]] ;;
        show)
            if [[ $2 == --property=Result ]]; then
                if [[ $FAIL_STAGE == stop-result ]]; then printf 'timeout\n'; else printf 'success\n'; fi
            elif [[ -e $TEST_ROOT/stopped ]]; then printf 'inactive\n'; else printf 'active\n'; fi ;;
    esac
}
docker() { record docker-state; if [[ $FAIL_STAGE == leftover ]]; then printf 'mock-container-id\n'; fi; }
date() { printf '20260920T030000000000001Z\n'; }
mktemp() { local path="$TEST_ROOT/backups/.backup-incomplete-test.tar.gz"; : > "$path"; printf '%s\n' "$path"; }
tar() { record tar; [[ $FAIL_STAGE != tar ]]; }
gzip() { record gzip; [[ $FAIL_STAGE != gzip ]]; }
sync() { record sync; }
find() { record retention; }
'''
            result = subprocess.run(["bash", "-c", mocks + source], capture_output=True, text=True,
                                    env={"PATH": os.defpath, "EVENT_LOG": str(root / "events"),
                                         "TEST_ROOT": str(root), "FAIL_STAGE": failure})
            events = (root / "events").read_text().splitlines()
            names = sorted(path.name for path in (root / "backups").iterdir())
            return result, events, names

    def test_backup_success_releases_lock_before_restart(self):
        result, events, names = self.run_mock_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("backup-20260920T030000000000001Z.tar.gz", names)
        self.assertLess(events.index("systemctl stop business-tools.service"), events.index("tar"))
        self.assertLess(events.index("flock --unlock 8"), events.index("systemctl start business-tools.service"))

    def test_backup_failures_restart_and_never_publish_partial_archive(self):
        for failure in ("stop", "stop-result", "leftover", "tar", "gzip"):
            with self.subTest(failure=failure):
                result, events, names = self.run_mock_backup(failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("systemctl start business-tools.service", events)
                self.assertEqual(names, [])
                if failure in ("stop", "stop-result", "leftover"):
                    self.assertNotIn("tar", events)

    def test_restart_failure_is_reported_even_after_successful_archive(self):
        result, events, names = self.run_mock_backup("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl start business-tools.service", events)
        self.assertEqual(len(names), 1)


if __name__ == "__main__":
    unittest.main()