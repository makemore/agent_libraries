#!/bin/bash
set -euo pipefail
umask 077

[[ $EUID -eq 0 ]] || { printf '%s\n' 'Backup requires root.' >&2; exit 1; }
exec 9>/run/ai-gateway-backup.lock
flock -x 9

python3 - <<'PY'
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


def backup():
    data = Path("/srv/ai-gateway")
    expected = Path("/dev/disk/by-id/google-ai-gateway-data")
    subprocess.run(["mountpoint", "--quiet", str(data)], check=True, capture_output=True)
    if not stat.S_ISBLK(expected.stat().st_mode) or data.stat().st_dev != expected.stat().st_rdev:
        raise ValueError("Data disk is not mounted")
    source = data / "bifrost"
    backups = data / "backups"
    if any(path.is_symlink() or not path.is_dir() for path in (data, source, backups)):
        raise ValueError("Unsafe backup directories")
    backups.chmod(0o700)
    databases = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Unexpected symbolic link")
        if path.suffix not in (".db", ".sqlite", ".sqlite3"):
            continue
        relative = path.relative_to(source)
        if not path.is_file() or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in relative.parts):
            raise ValueError("Unsafe database path")
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ValueError("Unexpected database format")
        databases.append(path)
    if not databases:
        raise ValueError("No databases to back up")

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    temporary = Path(tempfile.mkdtemp(prefix=".pending-" + timestamp + "-", dir=backups))
    try:
        for database in databases:
            destination = temporary / database.relative_to(source)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # SQLite's online backup includes committed WAL transactions. Never
            # copy live database/WAL files. Every database is consistent, though
            # separate databases do not share one cross-database transaction.
            subprocess.run(
                ["sqlite3", "-readonly", "-cmd", ".timeout 30000", str(database),
                 ".backup '" + str(destination) + "'"],
                check=True, capture_output=True,
            )
            destination.chmod(0o600)
            with destination.open("rb") as stream:
                os.fsync(stream.fileno())
        os.replace(temporary, backups / ("sqlite-" + timestamp))
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)

    # Only prune our completed, timestamp-named backups, after a successful new
    # backup. No config, env file, or encryption key is copied: retain the key's
    # Secret Manager versions separately for as long as backups need restoring.
    cutoff = now - timedelta(days=14)
    for old in backups.iterdir():
        if old.is_symlink() or not old.is_dir() or not re.fullmatch(r"sqlite-[0-9]{8}T[0-9]{12}Z", old.name):
            continue
        created = datetime.strptime(old.name, "sqlite-%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
        if created < cutoff:
            shutil.rmtree(old)


try:
    backup()
except Exception:
    # SQLite diagnostics can include application content; never emit them.
    print("AI gateway SQLite backup failed.", file=sys.stderr)
    sys.exit(1)
PY