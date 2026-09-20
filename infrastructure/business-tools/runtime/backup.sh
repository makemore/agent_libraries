#!/bin/bash
set -euo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID -eq 0 ]] || { printf '%s\n' 'Backup requires root.' >&2; exit 1; }

# Policy: daily COLD backup, with downtime for the WHOLE merged project including
# PG/MariaDB/SQLite/Redis/Caddy. Shutdown alone may take >1h. No off-host copy or
# disaster recovery guarantee: these archives share the source disk's failure fate.
# Use systemctl, not a second Compose owner. Manual maintenance must use this same
# maintenance lock; the application only holds the separate shared data lock.
[[ ! -L /run/business-tools-maintenance.lock && ! -L /run/business-tools-data.lock ]]
exec 9>/run/business-tools-maintenance.lock
flock --exclusive 9
python3 /opt/business-tools/prepare-disk.py --check-backup
[[ $(systemctl show --property=ActiveState --value business-tools.service) == active ]] || {
    printf '%s\n' 'Backup requires an active business-tools service.' >&2; exit 1;
}

partial=
restart_needed=0
data_locked=0
cleanup() {
    local result=$?
    trap - EXIT INT TERM HUP
    # A cleanup error must never skip the restart attempt.
    set +e
    if [[ -n $partial ]]; then
        # Only the exact mktemp-created incomplete archive; never a wildcard.
        rm -f -- "$partial" || result=1
    fi
    if (( data_locked )); then flock --unlock 8 || result=1; fi
    exec 8>&-
    if (( restart_needed )); then
        systemctl start business-tools.service >/dev/null 2>&1 || result=1
    fi
    if (( result != 0 )); then printf '%s\n' 'Business-tools backup failed; check service state.' >&2; fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
restart_needed=1
# A failed/timeout stop must NOT produce an archive claiming consistency.
systemctl stop business-tools.service
# Explicit exit also handles macOS Bash 3.2's errexit/command-substitution quirk
# in the isolated failure-mode tests; don't rely on implicit errexit here.
[[ $(systemctl show --property=ActiveState --value business-tools.service) == inactive ]] || exit 1
[[ $(systemctl show --property=Result --value business-tools.service) == success ]] || exit 1
exec 8>/run/business-tools-data.lock
flock --exclusive --nonblock 8
data_locked=1
# Check actual engine state too; a killed Compose client is not a cold database.
remaining=$(docker ps --all --quiet --filter label=com.docker.compose.project=business-tools 2>/dev/null)
[[ -z $remaining ]] || exit 1
python3 /opt/business-tools/prepare-disk.py --check-backup

stamp=$(date -u +%Y%m%dT%H%M%S%NZ)
archive="/srv/business-tools/backups/backup-$stamp.tar.gz"
[[ ! -e $archive && ! -L $archive ]]
partial=$(mktemp /srv/business-tools/backups/.backup-incomplete-XXXXXXXX.tar.gz)
# RuntimeDirectoryPreserve=yes keeps root-only env keys after stop. Include them
# with ALL on-disk keys/media/config/databases, not just DB exports. Tar preserves
# symlinks as links (never dereference); exclude only the backup directory itself.
tar --create --gzip --file "$partial" --numeric-owner --acls --xattrs \
    --one-file-system --exclude='./srv/business-tools/backups' \
    --directory / ./srv/business-tools ./run/business-tools >/dev/null 2>&1
gzip --test "$partial" >/dev/null 2>&1
chmod 0600 "$partial"
sync --file-system "$partial"
# Same-directory hard link publishes without overwriting any preexisting name.
ln -- "$partial" "$archive"
rm -- "$partial"
partial=

# Retain completed daily archives for 14 days; never follow symlinks, recurse, or
# match user filenames, incomplete files, other owners, or linked files.
find /srv/business-tools/backups -xdev -regextype posix-extended -mindepth 1 -maxdepth 1 \
    -type f -uid 0 -gid 0 -links 1 \
    -regex '/srv/business-tools/backups/backup-[0-9]{8}T[0-9]{15}Z\.tar\.gz' \
    -mmin +20160 -delete
# EXIT trap releases the data lock BEFORE starting the service, even on failure.