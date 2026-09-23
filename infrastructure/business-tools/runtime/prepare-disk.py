#!/usr/bin/python3
"""Fail-closed disk preparation. Only main() uses the fixed production paths.

Commands capture diagnostics rather than printing device/configuration contents.
Tests replace run(), device identity and paths; they never mount or format disks.
"""

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


DATA_DEVICE = Path("/dev/disk/by-id/google-business-tools-data")
DATA_MOUNT = Path("/srv/business-tools")
FSTAB = Path("/etc/fstab")
SOURCE_ROOT = Path("/opt/business-tools")
RUNTIME_ROOT = Path("/run/business-tools")
APPS = ("agentic-social", "mautic", "actual-budget", "invoice-ninja", "observability")
ENV_FILES = (
    "postiz.env", "postiz-db.env", "temporal.env", "temporal-db.env",
    "mautic.env", "mautic-db.env", "invoice-ninja.env", "invoice-db.env", "grafana.env",
)
# Parents grant traversal, not listing, to service UIDs. Leaf identities/modes
# follow the app READMEs. MariaDB entrypoints own their initial ownership change.
DIRECTORIES = (
    ("postiz", 0, 0, 0o711),
    ("postiz/config", 0, 0, 0o700),
    ("postiz/uploads", 0, 0, 0o755),
    ("postiz/postgres", 999, 999, 0o700),
    ("postiz/redis", 999, 999, 0o700),
    ("postiz/temporal-postgres", 999, 999, 0o700),
    ("mautic", 0, 0, 0o711),
    ("mautic/config", 33, 33, 0o700),
    ("mautic/logs", 33, 33, 0o750),
    ("mautic/media", 0, 0, 0o711),
    ("mautic/media/files", 33, 33, 0o750),
    ("mautic/media/images", 33, 33, 0o750),
    ("mautic/mariadb", 0, 0, 0o700),
    ("actual-budget", 0, 0, 0o711),
    ("actual-budget/data", 1001, 1001, 0o700),
    ("invoice-ninja", 0, 0, 0o711),
    ("invoice-ninja/public", 33, 33, 0o755),
    ("invoice-ninja/storage", 33, 33, 0o755),
    ("invoice-ninja/mysql", 0, 0, 0o700),
    ("invoice-ninja/redis", 999, 999, 0o700),
    ("observability", 0, 0, 0o711),
    ("observability/grafana", 472, 0, 0o700),
    ("observability/prometheus", 65534, 65534, 0o700),
    ("caddy", 0, 0, 0o711),
    ("caddy/data", 0, 0, 0o700),
    ("caddy/config", 0, 0, 0o700),
    ("backups", 0, 0, 0o700),
)


def require(condition):
    if not condition:
        raise ValueError("Unsafe business-tools host state")


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def no_symlinks(path):
    """Check every existing component, including dangling symlinks."""
    for component in (*reversed(path.parents), path):
        require(not component.is_symlink())
        if component != path:
            require(component.is_dir())


def root_owned(path, *, directory=False, mode=None):
    no_symlinks(path)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    require(info.st_uid == 0 and info.st_gid == 0 and not info.st_mode & 0o022)
    if not directory:
        require(info.st_nlink == 1)
    if mode is not None:
        require(stat.S_IMODE(info.st_mode) == mode)
    return info


def device_identity(expected):
    require(expected.is_symlink())
    info = expected.stat()
    require(stat.S_ISBLK(info.st_mode))
    return str(expected.resolve(strict=True)), info.st_rdev


def nodes(tree):
    for node in tree:
        yield node
        yield from nodes(node.get("children", []))


def inventory():
    disks = json.loads(run(
        "lsblk", "--json", "--paths", "--output", "NAME,TYPE,FSTYPE,MOUNTPOINTS,UUID,RO"
    ))["blockdevices"]
    mounts = json.loads(run(
        "findmnt", "--json", "--list", "--output", "TARGET,SOURCE,FSTYPE,UUID,OPTIONS"
    ))["filesystems"]
    return disks, mounts


def uuid_valid(value):
    return isinstance(value, str) and re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ) is not None


def same_source(source, device, uuid):
    if source.startswith("/dev/"):
        return os.path.realpath(source) == device
    if source.startswith("UUID="):
        return bool(uuid) and source == "UUID=" + uuid
    if source.startswith(("LABEL=", "PARTUUID=", "PARTLABEL=")):
        # Resolve aliases too: a label must not hide a second fstab use of this disk.
        return os.path.realpath(run("findfs", source)) == device
    return False


def parse_fstab(contents):
    entries = []
    for line in contents.splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#"):
            require(len(fields) >= 4)
            entries.append([
                re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), field)
                for field in fields
            ])
    return entries


def check_fstab(contents, device, data, uuid, blank):
    entries = parse_fstab(contents)
    at_target = []
    for entry in entries:
        target = os.path.normpath(entry[1])
        require(not target.startswith(str(data) + "/"))
        require(not (entry[0] == str(data) or entry[0].startswith(str(data) + "/")))
        if same_source(entry[0], device, uuid):
            require(target == str(data))
        if target == str(data):
            at_target.append(entry)
    require(len(at_target) <= 1)
    if at_target:
        entry = at_target[0]
        require(not blank and uuid_valid(uuid))
        require(entry[0] == "UUID=" + uuid and entry[2] == "ext4")
        require(not {"bind", "rbind", "ro", "noauto", "nofail", "loop", "remount"}.intersection(
            entry[3].split(",")
        ))
    return bool(at_target)


def check_mounts(mounts, device, data, uuid):
    at_target = []
    for item in mounts:
        target = os.path.normpath(item["target"])
        require(not target.startswith(str(data) + "/"))
        same = same_source(item["source"], device, uuid)
        if same or (uuid and item.get("uuid") == uuid):
            require(target == str(data))
        if target == str(data):
            at_target.append(item)
            require(same and item["fstype"] == "ext4")
            require("rw" in item["options"].split(","))
    require(len(at_target) <= 1)
    return bool(at_target)


def append_fstab(fstab, contents, uuid, data):
    info = root_owned(fstab)
    descriptor, temporary = tempfile.mkstemp(prefix=".business-tools-fstab-", dir=fstab.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(contents.rstrip("\n") + "\nUUID=" + uuid + " " + str(data) + " ext4 defaults 0 2\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, fstab)
    finally:
        Path(temporary).unlink(missing_ok=True)


def make_directory(path, uid, gid, mode):
    no_symlinks(path)
    if not path.exists():
        path.mkdir(mode=mode)
        # Change ownership/mode ONLY on the directory just created, never its tree.
        os.chown(path, uid, gid, follow_symlinks=False)
        path.chmod(mode)
    require(path.is_dir())


def prepare_directories(data, *, check_only=False):
    parents = {str(Path(relative).parent) for relative, _, _, _ in DIRECTORIES} - {"."}
    root_owned(data, directory=True)
    require(data.stat().st_mode & 0o001)  # Preserve existing modes; refuse bad traversal.
    device = data.stat().st_dev
    for relative, uid, gid, mode in DIRECTORIES:
        path = data / relative
        if not check_only:
            make_directory(path, uid, gid, mode)
        no_symlinks(path)
        require(path.is_dir() and path.stat().st_dev == device)
        if relative in parents:
            info = root_owned(path, directory=True)
            require(info.st_mode & 0o001)
        if relative == "backups":
            root_owned(path, directory=True, mode=0o700)


def prepare(*, check_only=False):
    root_owned(SOURCE_ROOT / "deployment.json", mode=0o600)
    deployment = json.loads((SOURCE_ROOT / "deployment.json").read_text(encoding="utf-8"))
    require(deployment["data_device"] == str(DATA_DEVICE))
    require(deployment["data_mount"] == str(DATA_MOUNT))
    device, device_number = device_identity(DATA_DEVICE)
    tree, mounts = inventory()
    candidates = [item for item in tree if item["name"] == device]
    require(len(candidates) == 1)
    disk = candidates[0]
    require(disk["type"] == "disk" and disk.get("ro") is False)
    require(not any({"/", "/boot", "/boot/efi"}.intersection(item.get("mountpoints") or [])
                    for item in nodes([disk])))
    require(not disk.get("children"))
    require(all(point in (None, str(DATA_MOUNT)) for point in disk.get("mountpoints") or []))
    mounted = check_mounts(mounts, device, DATA_MOUNT, disk.get("uuid"))
    no_symlinks(DATA_MOUNT)
    root_owned(DATA_MOUNT.parent, directory=True)
    require(DATA_MOUNT.parent.stat().st_mode & 0o001)
    if DATA_MOUNT.exists():
        root_owned(DATA_MOUNT, directory=True)
        require(DATA_MOUNT.stat().st_mode & 0o001)
        if not mounted:
            require(not any(DATA_MOUNT.iterdir()))
    signatures = json.loads(run("wipefs", "--no-act", "--json", device))["signatures"]
    require(signatures is None or isinstance(signatures, list))
    blank = not signatures and not disk.get("fstype") and not disk.get("uuid")
    if not blank:
        require(disk.get("fstype") == "ext4" and uuid_valid(disk.get("uuid")))
        require(len(signatures or []) == 1 and signatures[0].get("type") == "ext4")
    require(not blank or (not mounted and not any(disk.get("mountpoints") or [])))
    root_owned(FSTAB)
    contents = FSTAB.read_text(encoding="utf-8")
    recorded = check_fstab(contents, device, DATA_MOUNT, disk.get("uuid"), blank)
    if check_only:
        require(not blank and mounted and recorded)
    if blank:
        run("mkfs.ext4", device)  # No -F: never override a kernel/tool safety refusal.
    require(run("blkid", "-s", "TYPE", "-o", "value", device) == "ext4")
    uuid = run("blkid", "-s", "UUID", "-o", "value", device)
    require(uuid_valid(uuid) and (blank or uuid == disk["uuid"]))
    devices = run("blkid", "-t", "UUID=" + uuid, "-o", "device").splitlines()
    require(len(devices) == 1 and os.path.realpath(devices[0]) == device)
    if not recorded:
        append_fstab(FSTAB, contents, uuid, DATA_MOUNT)
    if not check_only:
        make_directory(DATA_MOUNT, 0, 0, 0o711)
    if not mounted:
        run("mount", str(DATA_MOUNT))
    run("mountpoint", "--quiet", str(DATA_MOUNT))
    require(DATA_MOUNT.stat().st_dev == device_number)
    _, actual_mounts = inventory()
    require(check_mounts(actual_mounts, device, DATA_MOUNT, uuid))
    prepare_directories(DATA_MOUNT, check_only=check_only)


def check_assets():
    names = (
        "bootstrap.sh", "prepare-disk.py", "compose.sh", "backup.sh", "fetch-secrets.py",
        "prepare-mautic.py", "stop.py",
        "business-tools.service", "business-tools-backup.service", "business-tools-backup.timer",
        "deployment.json", "compose.yaml", "Caddyfile", "invoice-ninja/nginx.conf",
        "invoice-ninja/logrotate.conf", "observability/prometheus.yml",
        "observability/provisioning/datasources/prometheus.yaml",
        *(app + "/compose.yaml" for app in APPS),
    )
    for name in names:
        path = SOURCE_ROOT / name
        for parent in path.parents:
            root_owned(parent, directory=True)
        root_owned(path)
    for directory in (Path("/etc/systemd/system"), Path("/etc/logrotate.d")):
        no_symlinks(directory)
        if directory.exists():
            root_owned(directory, directory=True)
    for name in ("business-tools.service", "business-tools-backup.service", "business-tools-backup.timer"):
        path = Path("/etc/systemd/system") / name
        no_symlinks(path)
        if path.exists():
            root_owned(path)
    target = Path("/etc/logrotate.d/business-tools-invoice")
    if target.parent.exists():
        no_symlinks(target)
        if target.exists():
            root_owned(target)


def check_backup():
    prepare(check_only=True)
    root_owned(RUNTIME_ROOT, directory=True, mode=0o700)
    for name in ENV_FILES:
        root_owned(RUNTIME_ROOT / name, mode=0o600)
    # No links, devices or subdirectories can smuggle other host data into tar.
    for path in RUNTIME_ROOT.iterdir():
        root_owned(path, mode=0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true")
    group.add_argument("--check-assets", action="store_true")
    group.add_argument("--check-backup", action="store_true")
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0)
        if args.check_assets:
            check_assets()
        elif args.check_backup:
            check_backup()
        else:
            prepare(check_only=args.check)
    except Exception:
        print("Business-tools host safety check failed; refusing to continue.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())