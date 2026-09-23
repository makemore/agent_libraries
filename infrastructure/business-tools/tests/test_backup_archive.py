"""Real archive file coverage/integrity, not PostgreSQL/MariaDB runtime restore.

Only synthetic temporary data is used; disk preparation and backup.sh's lifecycle
are never executed. Numeric ownership checks cover only the fixture UID/GID, not
all production service identities (including when run unprivileged). ACL/xattr
preservation is not tested or claimed. Native tar/gzip perform the roundtrip.
"""

from contextlib import closing
import importlib.util
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"


class BackupArchiveTests(unittest.TestCase):
    def run_command(self, command, cwd):
        # Ignore caller TAR_OPTIONS/GZIP settings; only isolated fixtures are read.
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                env={"PATH": os.environ.get("PATH", os.defpath),
                                     "LC_ALL": "C"}, timeout=30)
        diagnostic = result.stderr.lower()
        if result.returncode and "option" in diagnostic and any(
                word in diagnostic for word in ("unrecognized", "unknown", "unsupported", "not supported")):
            self.skipTest("Native archive tool does not support the required options")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_production_archive_roundtrip(self):
        tar, gzip = shutil.which("tar"), shutil.which("gzip")
        if not tar or not gzip:
            self.skipTest("Native tar and gzip are required")
        spec = importlib.util.spec_from_file_location("archive_disk_layout", RUNTIME / "prepare-disk.py")
        disk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(disk)  # Constants only; never call prepare/main.
        script = (RUNTIME / "backup.sh").read_text().replace("\\\n", "")
        commands = [shlex.split(line) for line in script.splitlines() if line.startswith("tar ")]
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertEqual(command[-2:], [">/dev/null", "2>&1"])
        command = command[:-2]  # Capture diagnostics with subprocess, not a shell.
        destination = command.index("--file") + 1
        directory = command.index("--directory") + 1
        self.assertEqual(command[destination], "$partial")
        self.assertEqual(command[directory:], ["/", "./srv/business-tools", "./run/business-tools"])
        self.assertEqual(command.count("--directory"), 1)
        self.assertEqual(command.count("--file"), 1)

        with tempfile.TemporaryDirectory(prefix="business-tools-archive-") as temporary:
            root = Path(temporary).resolve()
            source, restored = root / "source", root / "restored"
            data = source / disk.DATA_MOUNT.relative_to("/")
            keys = source / disk.RUNTIME_ROOT.relative_to("/")
            data.mkdir(parents=True)
            data.chmod(0o711)
            keys.mkdir(parents=True)
            keys.chmod(0o700)

            def write(path, contents, mode=0o600):
                path.write_bytes(contents)
                path.chmod(mode)

            directories = [Path(item[0]) for item in disk.DIRECTORIES]
            backups = data / "backups"
            for relative, _, _, mode in disk.DIRECTORIES:
                path = data / relative
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(mode)  # Keep fixture ownership, not production service UIDs.
                if path != backups and not any(Path(relative) in other.parents for other in directories):
                    write(path / "fixture-marker", (relative + "\n").encode())
            self.assertEqual(len(disk.ENV_FILES), 9)
            for name in disk.ENV_FILES:
                write(keys / name, ("ARCHIVE_TEST_MARKER=" + name + "\n").encode())
            write(data / "postiz/config/fixture.json", b'{"archive_test": true}\n')
            write(data / "caddy/data/fixture.key", b"synthetic-not-a-private-key\n")
            media = data / "postiz/uploads/media.bin"
            write(media, bytes(range(256)) + b"\x00archive-media\xff", 0o640)
            link = media.with_name("latest")
            link.symlink_to(media.name)
            database = data / "actual-budget/data/fixture.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker VALUES (?)", ("archive-roundtrip",))
                connection.commit()
            database.chmod(0o600)
            (backups / "nested/deeper").mkdir(parents=True)
            write(backups / "old-backup-sentinel", b"excluded old backup sentinel")
            write(backups / "nested/deeper/sentinel", b"excluded nested sentinel")
            archive = backups / ".backup-incomplete-fixture.tar.gz"
            archive.touch(mode=0o600)  # Production mktemp destination is also excluded.
            command[0], command[destination], command[directory] = tar, str(archive), str(source)
            self.run_command(command, source)
            self.run_command([gzip, "--test", str(archive)], source)

            expected = [data, keys, *data.rglob("*"), *keys.iterdir()]
            expected = {path.relative_to(source): path for path in expected
                        if not path.is_relative_to(backups)}
            with tarfile.open(archive, "r:gz") as contents:
                entries = contents.getmembers()
                members = {Path(member.name): member for member in entries}
                self.assertEqual(len(members), len(entries))  # Validate every entry before extraction.
                self.assertTrue(expected.keys() <= members.keys())
                for name, member in members.items():
                    self.assertFalse(name.is_absolute() or ".." in name.parts)
                    self.assertFalse(name.is_relative_to(backups.relative_to(source)))
                    self.assertTrue(member.isfile() or member.isdir() or member.issym())
                    if member.issym():
                        self.assertEqual((name, member.linkname), (link.relative_to(source), media.name))
                for name, original in expected.items():
                    info = original.lstat()
                    self.assertEqual((members[name].uid, members[name].gid), (info.st_uid, info.st_gid))

            restored.mkdir()  # Always new, never over a live tree or existing files.
            self.run_command([tar, "--extract", "--gzip", "--file", str(archive),
                              "--numeric-owner", "--same-permissions", "--directory", str(restored)], root)
            self.assertFalse((restored / backups.relative_to(source)).exists())
            for name, original in expected.items():
                with self.subTest(path=str(name)):
                    target = restored / name
                    before, after = original.lstat(), target.lstat()
                    self.assertEqual(stat.S_IFMT(after.st_mode), stat.S_IFMT(before.st_mode))
                    self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
                    if original.is_symlink():
                        self.assertEqual(os.readlink(target), media.name)
                    else:
                        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
                        if original.is_file():
                            self.assertEqual(target.read_bytes(), original.read_bytes())
            self.assertEqual((restored / link.relative_to(source)).read_bytes(), media.read_bytes())
            uri = (restored / database.relative_to(source)).as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchall(),
                                 [("archive-roundtrip",)])


if __name__ == "__main__":
    unittest.main()