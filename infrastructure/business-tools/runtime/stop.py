#!/usr/bin/python3
"""Fixed-purpose ExecStop: stop ingress, stop all, await monitors, remove."""

from contextlib import contextmanager
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import time


SELF_CGROUP = Path("/proc/self/cgroup")
UNIT_CGROUP = "/system.slice/business-tools.service"
CGROUP_PROCS = Path("/sys/fs/cgroup/system.slice/business-tools.service/cgroup.procs")
COMPOSE = "/opt/business-tools/compose.sh"
CONTAINERS = ("/usr/bin/docker", "ps", "--all", "--quiet", "--filter",
              "label=com.docker.compose.project=business-tools")
# systemd owns the overall 4200s deadline. This bounds only the pidfd barrier;
# do not override Compose's per-service stop grace periods with a CLI timeout.
BARRIER_TIMEOUT = 4200


def command(*args, capture=False):
    return subprocess.run(
        args, check=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    ).stdout


def unit_pids():
    """Read only numeric cgroup metadata, never process argv or environments."""
    pids = set()
    for line in CGROUP_PROCS.read_text(encoding="ascii").splitlines():
        if not line.isdecimal() or int(line) <= 0:
            raise ValueError("Invalid unit inventory")
        pids.add(int(line))
    if os.getpid() not in pids:
        raise ValueError("Helper missing from unit inventory")
    return pids


def close_pidfds(pidfds):
    failed = False
    for fd in sorted(set(pidfds)):
        try:
            os.close(fd)
        except OSError:
            failed = True
    if failed:
        raise OSError("Pidfd cleanup failed")


@contextmanager
def original_processes():
    pidfds = {}
    try:
        # Capture ALL unit processes (Docker CLI and Compose plugin) before
        # starting any helper child. cgroup.procs may repeat PIDs. MAINPID is
        # deliberately irrelevant, including when the main process already exited.
        pids = unit_pids() - {os.getpid()}
        for pid in sorted(pids):
            try:
                pidfds[pid] = os.pidfd_open(pid, 0)
            except ProcessLookupError:
                # Already exited. Never reopen or probe this numeric PID: it may
                # be reused. The final inventory independently rejects newcomers.
                continue
        # Close the inventory/open PID-reuse race before starting any commands.
        # Check membership AFTER opening handles, then readiness AFTER membership:
        # a live fd must still name a unit member, not a reused PID elsewhere.
        members = unit_pids() - {os.getpid()}
        if members - pids:
            raise ValueError("Unexpected unit process")
        exited = set()
        if pidfds:
            poller = select.poll()
            for fd in pidfds.values():
                poller.register(fd, select.POLLIN)
            exited = ready_pidfds(poller, set(pidfds.values()), 0)
        if any(pid not in members and fd not in exited for pid, fd in pidfds.items()):
            raise ValueError("Process left unit during capture")
        yield set(pidfds.values())
    finally:
        close_pidfds(pidfds.values())


def ready_pidfds(poller, pending, timeout):
    exited = set()
    for fd, events in poller.poll(timeout):
        if (fd not in pending or events & (select.POLLERR | select.POLLNVAL)
                or not events & select.POLLIN):
            raise OSError("Invalid pidfd readiness")
        exited.add(fd)
    return exited


def wait_for_exit(pidfds):
    pending = set(pidfds)
    if not pending:
        return
    poller = select.poll()
    for fd in pending:
        poller.register(fd, select.POLLIN)
    deadline = time.monotonic() + BARRIER_TIMEOUT
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Monitor exit barrier expired")
        for fd in ready_pidfds(poller, pending, math.ceil(remaining * 1000)):
            pending.remove(fd)
            poller.unregister(fd)


def main():
    try:
        if os.geteuid() != 0:
            return 1
        if SELF_CGROUP.read_text(encoding="ascii").splitlines() != ["0::" + UNIT_CGROUP]:
            return 1
        # No data/maintenance lock here: the foreground monitor/backup owns them.
        with original_processes() as pidfds:
            # Let ingress drain first. Never signal the foreground monitors:
            # Compose cancellation can return 130 even for requested shutdown.
            # A genuine first-container failure still reaches systemd unchanged.
            command(COMPOSE, "stop", "caddy")
            command(COMPOSE, "stop")
            wait_for_exit(pidfds)
            if unit_pids() != {os.getpid()}:
                return 1
            command(COMPOSE, "down", "--remove-orphans")
            if command(*CONTAINERS, capture=True).strip():
                return 1
        # Cleanup success does not reset systemd's recorded main-process failure.
        return 0
    except (Exception, KeyboardInterrupt):
        # Commands, engine output and exception text may contain private data.
        return 1


if __name__ == "__main__":
    sys.exit(main())