#!/bin/bash
set -euo pipefail
umask 077

[[ $EUID -eq 0 ]] || { printf '%s\n' 'Bootstrap requires root.' >&2; exit 1; }
exec 9>/run/ai-gateway-bootstrap.lock
flock -x 9

# Ubuntu packages only; never execute a downloaded installer.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 python3 sqlite3

python3 - <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def require(condition):
    if not condition:
        raise ValueError("Unsafe data disk state")


def nodes(tree):
    for node in tree:
        yield node
        yield from nodes(node.get("children", []))


def unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def prepare():
    spec = importlib.util.spec_from_file_location("gateway_secrets", "/opt/ai-gateway/fetch-secrets.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    deployment = helper.load_deployment()
    # The helper validates these exact constants against deployment.json.
    expected = Path("/dev/disk/by-id/google-ai-gateway-data")
    data = Path("/srv/ai-gateway")
    require(deployment["data_device"] == str(expected) and deployment["data_mount"] == str(data))
    require(expected.is_symlink() and stat.S_ISBLK(expected.stat().st_mode))
    device = str(expected.resolve(strict=True))
    tree = json.loads(run("lsblk", "--json", "--paths", "--output", "NAME,TYPE,FSTYPE,MOUNTPOINTS,UUID"))["blockdevices"]
    disks = [item for item in tree if item["name"] == device]
    require(len(disks) == 1 and disks[0]["type"] == "disk")
    disk = disks[0]
    # Exclude both the boot disk and any partitioned/mapped data disk.
    boot_mounts = {"/", "/boot", "/boot/efi"}
    require(not any(boot_mounts.intersection(item.get("mountpoints") or []) for item in nodes([disk])))
    require(not disk.get("children"))
    require(all(point in (None, str(data)) for point in disk.get("mountpoints") or []))
    mounts = json.loads(run("findmnt", "--json", "--list", "--output", "TARGET,SOURCE,FSTYPE,UUID"))["filesystems"]
    require(not any(item["target"].startswith(str(data) + "/") for item in mounts))
    mounted = [item for item in mounts if item["target"] == str(data)]
    require(len(mounted) <= 1)
    for parent in (data.parent, data):
        require(not parent.is_symlink())
        require(not parent.exists() or parent.is_dir())
    if not mounted and data.exists():
        # Never hide files accidentally written to the boot disk.
        require(not any(data.iterdir()))

    signatures = json.loads(run("wipefs", "--no-act", "--json", device))["signatures"]
    require(signatures is None or isinstance(signatures, list))
    blank = not signatures and not disk.get("fstype")
    if not blank:
        require(disk.get("fstype") == "ext4" and len(signatures or []) == 1)
        require(signatures[0].get("type") == "ext4")
    require(not blank or not mounted)
    if mounted:
        require(mounted[0]["fstype"] == "ext4")
        require(os.path.realpath(mounted[0]["source"]) == device)

    fstab = Path("/etc/fstab")
    require(not fstab.is_symlink() and fstab.is_file())
    contents = fstab.read_text(encoding="utf-8")
    entries = []
    for line in contents.splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#"):
            require(len(fields) >= 4)
            entries.append([unescape(field) for field in fields])
    at_target = [entry for entry in entries if entry[1] == str(data)]
    require(len(at_target) <= 1)
    require(not any(entry[1].startswith(str(data) + "/") for entry in entries))
    for entry in entries:
        same_device = os.path.realpath(entry[0]) == device
        same_uuid = bool(disk.get("uuid")) and entry[0] == "UUID=" + disk["uuid"]
        if same_device or same_uuid:
            require(entry[1] == str(data))
    if at_target:
        entry = at_target[0]
        require(not blank and bool(disk.get("uuid")))
        require(entry[0] == "UUID=" + disk["uuid"] and entry[2] == "ext4")
        require(not {"bind", "rbind", "ro", "noauto", "nofail"}.intersection(entry[3].split(",")))

    if blank:
        # No force flag, no partitioning, no erasure of existing signatures.
        run("mkfs.ext4", device)
    require(run("blkid", "-s", "TYPE", "-o", "value", device) == "ext4")
    uuid = run("blkid", "-s", "UUID", "-o", "value", device)
    require(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid))
    if not at_target:
        info = fstab.stat()
        descriptor, temporary = tempfile.mkstemp(prefix=".ai-gateway-fstab-", dir=fstab.parent)
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
    data.mkdir(mode=0o755, exist_ok=True)
    if not mounted:
        run("mount", str(data))
    run("mountpoint", "--quiet", str(data))
    require(data.stat().st_dev == expected.stat().st_rdev)

    for relative, uid, gid, mode in (
        ("bifrost", 1000, 1000, 0o750),
        ("caddy", 0, 0, 0o700),
        ("caddy/data", 0, 0, 0o700),
        ("caddy/config", 0, 0, 0o700),
        ("backups", 0, 0, 0o700),
    ):
        path = data / relative
        require(not path.is_symlink())
        path.mkdir(mode=mode, exist_ok=True)
        os.chown(path, uid, gid)
        path.chmod(mode)
    config = data / "bifrost/config.json"
    require(not config.is_symlink())
    if not config.exists():
        # Atomic no-clobber seeding, including on interrupted bootstrap reruns.
        descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=config.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(Path("/opt/ai-gateway/config.json").read_bytes())
                os.fchown(stream.fileno(), 1000, 1000)
                os.fchmod(stream.fileno(), 0o600)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, config)
        finally:
            Path(temporary).unlink(missing_ok=True)
    require(config.is_file())
    os.chown(config, 1000, 1000)
    config.chmod(0o600)


try:
    prepare()
except Exception:
    print("AI gateway data preparation failed; refusing startup.", file=sys.stderr)
    sys.exit(1)
PY

chmod 0700 /opt/ai-gateway/bootstrap.sh /opt/ai-gateway/fetch-secrets.py /opt/ai-gateway/backup.sh
chmod 0600 /opt/ai-gateway/deployment.json /opt/ai-gateway/compose.yaml
chmod 0644 /opt/ai-gateway/config.json /opt/ai-gateway/Caddyfile /opt/ai-gateway/admin.caddy

# Non-secret verifier code/config are readable by a sandboxed dynamic user;
# never grant that user access to bootstrap.env, provider data or SQLite files.
install -d -o root -g root -m 0755 /usr/local/lib/ai-gateway
install -o root -g root -m 0644 /opt/ai-gateway/iap-auth.py /opt/ai-gateway/iap-auth.json /usr/local/lib/ai-gateway/
install -o root -g root -m 0644 /opt/ai-gateway/iap-auth.service /etc/systemd/system/ai-gateway-iap-auth.service

# Keep catalog code outside the inaccessible deployment/data/secret directories.
# System Python's standard library is sufficient; no dependencies or credentials.
# GCE OS Login's NSS lookup stalls DynamicUser allocation before exec. Use a
# dedicated local system account, never a shared login or a privileged user.
if ! getent passwd ai-gateway-catalog >/dev/null; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin ai-gateway-catalog
fi
python3 - <<'PY'
import pwd
account = pwd.getpwnam("ai-gateway-catalog")
if not (0 < account.pw_uid < 1000 and account.pw_dir == "/nonexistent" and account.pw_shell == "/usr/sbin/nologin"):
    raise SystemExit("Catalog service account must be a non-login system account")
PY
# Create the exact private runtime directory before masking it; hiding all /run
# breaks systemd's mount-namespace setup on this VM. Never expose bootstrap.env.
install -d -o root -g root -m 0700 /run/ai-gateway
install -o root -g root -m 0644 /opt/ai-gateway/model-catalog.py /usr/local/lib/ai-gateway/model-catalog.py
cat > /usr/local/lib/ai-gateway/model-catalog-ready.py <<'PY'
"""Wait for the guard's unauthenticated denial, without contacting Bifrost."""
import http.client
import time

for _ in range(20):
    connection = http.client.HTTPConnection("127.0.0.1", 9092, timeout=1)
    try:
        connection.request("GET", "/v1/models")
        if connection.getresponse().status == 401:
            break
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    time.sleep(0.25)
else:
    raise SystemExit(1)
PY
chown root:root /usr/local/lib/ai-gateway/model-catalog-ready.py
chmod 0644 /usr/local/lib/ai-gateway/model-catalog-ready.py

cat > /etc/systemd/system/ai-gateway-model-catalog.service <<'UNIT'
[Unit]
Description=AI gateway virtual-key model catalog guard
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=ai-gateway-catalog
Group=ai-gateway-catalog
ExecStart=/usr/bin/python3 -I /usr/local/lib/ai-gateway/model-catalog.py
# Readiness requires no key or upstream, so Bifrost can start after this unit.
ExecStartPost=/usr/bin/python3 -I /usr/local/lib/ai-gateway/model-catalog-ready.py
TimeoutStartSec=30
Restart=on-failure
RestartSec=10
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET
IPAddressDeny=any
IPAddressAllow=127.0.0.1/32
SocketBindDeny=any
SocketBindAllow=ipv4:tcp:9092
# Mask only gateway state/secrets, leaving systemd's own runtime paths usable.
InaccessiblePaths=/opt/ai-gateway /srv/ai-gateway /run/ai-gateway
MemoryMax=128M
TasksMax=32
LimitCORE=0
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/ai-gateway.service <<'UNIT'
[Unit]
Description=AI gateway (Bifrost and Caddy)
Wants=network-online.target
Requires=docker.service
After=network-online.target docker.service
# Wait for guard startup, but a catalog outage must not stop existing inference.
# Caddy's catalog proxy remains fail-closed (502) when the guard is unavailable.
Wants=ai-gateway-model-catalog.service
After=ai-gateway-model-catalog.service
RequiresMountsFor=/srv/ai-gateway
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory=/opt/ai-gateway
RuntimeDirectory=ai-gateway
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=restart
UMask=0077
ExecStartPre=/usr/bin/mountpoint --quiet /srv/ai-gateway
ExecStartPre=/usr/bin/python3 /opt/ai-gateway/fetch-secrets.py
# Compose must not set Docker restart policies: systemd owns mount/secret gating.
ExecStart=/usr/bin/docker compose --project-name ai-gateway --file /opt/ai-gateway/compose.yaml up --remove-orphans --abort-on-container-exit
ExecStop=/usr/bin/docker compose --project-name ai-gateway --file /opt/ai-gateway/compose.yaml down
Restart=always
RestartSec=30
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/ai-gateway-backup.service <<'UNIT'
[Unit]
Description=Online SQLite backups for AI gateway
RequiresMountsFor=/srv/ai-gateway
After=ai-gateway.service

[Service]
Type=oneshot
UMask=0077
ExecStartPre=/usr/bin/mountpoint --quiet /srv/ai-gateway
ExecStart=/bin/bash /opt/ai-gateway/backup.sh
UNIT

cat > /etc/systemd/system/ai-gateway-backup.timer <<'UNIT'
[Unit]
Description=Daily AI gateway backup before the 04:00 UTC disk snapshot

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true
Unit=ai-gateway-backup.service

[Install]
WantedBy=timers.target
UNIT

chmod 0644 /etc/systemd/system/ai-gateway.service /etc/systemd/system/ai-gateway-backup.{service,timer}
chown root:root /etc/systemd/system/ai-gateway-model-catalog.service
chmod 0644 /etc/systemd/system/ai-gateway-model-catalog.service
systemctl daemon-reload
if python3 -c 'import json,sys; sys.exit(0 if json.load(open("/usr/local/lib/ai-gateway/iap-auth.json"))["enabled"] else 1)'; then
    # Ubuntu 24.04 image already supplies these through its system packages.
    # Fail closed rather than auto-installing a dependency or rolling our own crypto.
    python3 -c 'import jwt, cryptography' || { printf '%s\n' 'IAP verifier libraries unavailable.' >&2; exit 1; }
    systemctl enable ai-gateway-iap-auth.service
    systemctl restart ai-gateway-iap-auth.service
else
    systemctl disable --now ai-gateway-iap-auth.service
fi
systemctl enable ai-gateway-model-catalog.service
systemctl restart ai-gateway-model-catalog.service
systemctl enable docker.service ai-gateway.service ai-gateway-backup.timer
systemctl start docker.service
systemctl restart ai-gateway-backup.timer
# Recreate the service on reruns to read current templates. A missing secret
# fails this start, but systemd keeps retrying ExecStartPre every 30 seconds.
systemctl restart ai-gateway.service