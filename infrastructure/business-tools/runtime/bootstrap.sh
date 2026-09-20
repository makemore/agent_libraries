#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID -eq 0 ]] || { printf '%s\n' 'Bootstrap requires root.' >&2; exit 1; }

metadata_firewall() {
    # Docker creates this chain. Refuse startup if its iptables backend is absent.
    iptables --wait -nL DOCKER-USER >/dev/null 2>&1
    for bridge in docker0 'br+'; do
        # Insert BEFORE Docker's terminal RETURN; appending after it is ineffective.
        # Only container bridge forwarding is affected. Host OUTPUT (secret fetch)
        # remains allowed; no INPUT/OUTPUT/FORWARD chain or global policy changes.
        iptables --wait --check DOCKER-USER --in-interface "$bridge" \
            --destination 169.254.169.254/32 --jump DROP >/dev/null 2>&1 ||
            iptables --wait --insert DOCKER-USER 1 --in-interface "$bridge" \
                --destination 169.254.169.254/32 --jump DROP >/dev/null 2>&1
    done
}

require_compose_version() {
    local compose_version
    compose_version=$(docker compose version --short 2>/dev/null) || {
        printf '%s\n' 'Docker Compose >= 2.30.0 is required; Ubuntu package is unavailable.' >&2
        return 1
    }
    # Accept stable SemVer build metadata (Ubuntu +ds packaging), not prereleases.
    if [[ ! $compose_version =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+)(\+[0-9A-Za-z.-]+)?$ ]] ||
        (( 10#${BASH_REMATCH[1]:-0} < 2 || (10#${BASH_REMATCH[1]:-0} == 2 && 10#${BASH_REMATCH[2]:-0} < 30) )); then
        printf '%s\n' 'Docker Compose >= 2.30.0 is required for raw env files. Upgrade the approved Ubuntu package; no fallback or env downgrade is permitted.' >&2
        return 1
    fi
}

if [[ ${1:-} == --metadata-firewall && $# -eq 1 ]]; then
    metadata_firewall
    exit 0
fi
[[ $# -eq 0 ]] || { printf '%s\n' 'Unknown bootstrap option.' >&2; exit 1; }

# Maintenance lock is NEVER acquired by the application unit. Data lock is
# acquired after stopping, released before starting: no stop/start lock inversion.
# Use root-writable /run, not the world-writable /run/lock directory.
[[ ! -L /run/business-tools-maintenance.lock && ! -L /run/business-tools-data.lock ]]
exec 9>/run/business-tools-maintenance.lock
flock --exclusive 9
grep -qx 'ID=ubuntu' /etc/os-release
grep -qx 'VERSION_ID="24.04"' /etc/os-release

# Terraform stages trusted root-owned sources with 0600 modes. Restore executable
# bits before asking an already-installed unit to stop through the new wrapper.
python3 /opt/business-tools/prepare-disk.py --check-assets
chmod 0700 /opt/business-tools/{bootstrap.sh,compose.sh,backup.sh,prepare-disk.py,fetch-secrets.py}
chmod 0644 /opt/business-tools/Caddyfile
if [[ $(systemctl show --property=LoadState --value business-tools.service) == loaded ]]; then
    systemctl stop business-tools.service
    [[ $(systemctl show --property=ActiveState --value business-tools.service) == inactive ]]
    [[ $(systemctl show --property=Result --value business-tools.service) == success ]]
fi
exec 8>/run/business-tools-data.lock
flock --exclusive --nonblock 8

# Ubuntu packages only; no downloaded installer or repository changes.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 python3 logrotate iptables
require_compose_version
systemctl start docker.service
metadata_firewall
# A dead Compose client does not prove that the databases stopped. Leave any
# unexpected project containers untouched and require operator recovery.
remaining=$(docker ps --all --quiet --filter label=com.docker.compose.project=business-tools 2>/dev/null)
[[ -z $remaining ]]
python3 /opt/business-tools/prepare-disk.py --check-assets
python3 /opt/business-tools/prepare-disk.py
# First-install only, while the stack is stopped and both locks are held.
# Existing operator config is never overwritten; conflicts require review.
python3 /opt/business-tools/prepare-mautic.py

# Ownership/symlink checks above cover both the source and existing destination.
# Validation output is suppressed; even debug output must not expose app data.
logrotate --debug /opt/business-tools/invoice-ninja/logrotate.conf >/dev/null 2>&1 || {
    printf '%s\n' 'Invoice logrotate validation failed.' >&2; exit 1;
}
install -o root -g root -m 0644 /opt/business-tools/invoice-ninja/logrotate.conf \
    /etc/logrotate.d/business-tools-invoice
for unit in business-tools.service business-tools-backup.service business-tools-backup.timer; do
    install -o root -g root -m 0644 "/opt/business-tools/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable docker.service business-tools.service business-tools-backup.timer
flock --unlock 8
exec 8>&-
systemctl start business-tools-backup.timer
# A missing secret fails ExecStartPre and retries with systemd's 30-second backoff.
systemctl restart business-tools.service