#!/usr/bin/python3
"""Approved one-time config-only repair; public {helper, unit} on stdin.
Preview unless --apply; both modes require existing stage/maintenance locks.
--previous-helper-sha256 authorizes only a reviewed helper-only replacement when
both units already have candidate semantics; it never authorizes a unit change.
Copies are NOT a data backup. Caller may start the existing backup service only
after success AND lock release. Never execute helper code, stop or blindly undo.
Needed until metadata stages the repair; covered by tests/test_stop_handoff.py.
"""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import warnings

ROOT, OWNER_UID, OWNER_GID = Path("/"), 0, 0
HELPER = Path("/opt/business-tools/stop.py")
STAGED = Path("/opt/business-tools/business-tools.service")
INSTALLED = Path("/etc/systemd/system/business-tools.service")
LOCKS = (Path("/run/business-tools-stage.lock"), Path("/run/business-tools-maintenance.lock"))
BACKUP_ROOT = Path("/var/tmp")
OLD = "ExecStop=/opt/business-tools/compose.sh down --remove-orphans"
NEW = "ExecStop=/usr/bin/python3 /opt/business-tools/stop.py"
LIMIT = 1024 * 1024


def require(condition):
    if not condition:
        raise ValueError("Stop repair refused")


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid options")


def identity(info):
    return tuple(getattr(info, "st_" + key) for key in
                 ("dev", "ino", "mode", "nlink", "uid", "gid", "size", "mtime_ns", "ctime_ns"))


def checked(path, directory=False):
    require(path.is_absolute() and path.is_relative_to(ROOT) and ".." not in path.parts)
    for node in (*reversed(path.parents), path):
        if not node.is_relative_to(ROOT):
            continue
        info = node.lstat()
        require(info.st_uid == OWNER_UID and info.st_gid == OWNER_GID)
        require(stat.S_ISDIR(info.st_mode) if node != path or directory else
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        # Sole writable-ancestor exception: mkdtemp's root-owned sticky parent.
        require(stat.S_IMODE(info.st_mode) == 0o1777 if node == BACKUP_ROOT else
                not info.st_mode & 0o7022)
    return info


def snapshot(path):
    try:
        before = identity(checked(path))
    except FileNotFoundError:
        return None
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        require(identity(os.fstat(stream.fileno())) == before)
        content = stream.read(LIMIT + 1)
        require(len(content) <= LIMIT and identity(os.fstat(stream.fileno())) == before)
    require(identity(checked(path)) == before)
    return before, content


def atomic(path, content, mode, expected):
    require(snapshot(path) == expected)
    checked(path.parent, directory=True)
    fd, temporary = tempfile.mkstemp(prefix=".stop-repair-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        require(snapshot(path) == expected)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def systemctl(*args):
    return subprocess.run(("/usr/bin/systemctl", *args), check=True, text=True,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, close_fds=True, timeout=30,
                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}).stdout.strip()


def show(property_name):
    return systemctl("show", "--property=" + property_name, "--value", "business-tools.service")


def running():
    require(show("DropInPaths") == "" and show("ActiveState") == "active" and show("SubState") == "running")
    pid = show("MainPID")
    require(re.fullmatch(r"[1-9][0-9]*", pid) is not None)
    return pid


def effective_stop():
    require(re.fullmatch(
        r"\{ path=/usr/bin/python3 ; argv\[\]=/usr/bin/python3 /opt/business-tools/stop\.py ; "
        r"ignore_errors=no ; start_time=[^;{}\r\n]* ; stop_time=[^;{}\r\n]* ; "
        r"pid=[0-9]+ ; code=[^;{}\r\n]* ; status=[^;{}\r\n]* \}", show("ExecStop")) is not None)


def semantic(content):
    lines = [line.strip() for line in content.decode("utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    require(b"\0" not in content and not any(line.endswith("\\") for line in lines))
    return lines


def main(argv=None):
    result = {"ok": False, "applied": False, "already_installed": False}
    try:
        parser = QuietParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--previous-helper-sha256")
        options = parser.parse_args(argv)
        previous_hash = options.previous_helper_sha256
        require(previous_hash is None or re.fullmatch(r"[0-9a-f]{64}", previous_hash) is not None)
        require(sys.platform == "linux" and os.geteuid() == 0)
        payload = json.loads(sys.stdin.read(2 * LIMIT + 1))
        require(type(payload) is dict and set(payload) == {"helper", "unit"})
        require(all(type(value) is str and 0 < len(value.encode("utf-8")) <= LIMIT for value in payload.values()))
        helper, unit = payload["helper"].encode("utf-8"), payload["unit"].encode("utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(helper, str(HELPER), "exec", dont_inherit=True)
        candidate = semantic(unit)
        require([line for line in candidate if line.split("=", 1)[0].strip() == "ExecStop"] == [NEW])
        require(next(line for line in reversed(candidate[:candidate.index(NEW)]) if line.startswith("[")) == "[Service]")
        with ExitStack() as stack:
            for path in LOCKS:  # Same order as metadata; close releases locks before printing.
                before = identity(checked(path))
                require(stat.S_IMODE(before[2]) == 0o600)
                stream = stack.enter_context(os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"))
                require(identity(os.fstat(stream.fileno())) == before)
                fcntl.flock(stream, fcntl.LOCK_EX)
                require(identity(checked(path)) == before)
            originals = {path: snapshot(path) for path in (HELPER, STAGED, INSTALLED)}
            previous = originals[STAGED]
            require(previous is not None and originals[INSTALLED] is not None and previous[1] == originals[INSTALLED][1])
            units_already = semantic(previous[1]) == candidate
            require(units_already or semantic(previous[1]) == [OLD if line == NEW else line for line in candidate])
            old_helper = originals[HELPER]
            require(previous_hash is None or (units_already and old_helper is not None))
            helper_changed = old_helper is not None and old_helper[1] != helper
            if helper_changed:
                require(units_already and previous_hash is not None and
                        hashlib.sha256(old_helper[1]).hexdigest() == previous_hash)
            require(not units_already or old_helper is not None)
            already = units_already and not helper_changed
            pid = running()
            require(all(snapshot(path) == before for path, before in originals.items()))
            if units_already:
                effective_stop()
                require(running() == pid)
            if options.apply and not already:
                checked(BACKUP_ROOT, directory=True)
                backup = Path(tempfile.mkdtemp(prefix="business-tools-stop-repair-", dir=BACKUP_ROOT))
                result["backup_dir"] = str(backup)
                require(stat.S_IMODE(checked(backup, directory=True).st_mode) == 0o700)
                copies = {"staged.service": previous[1], "installed.service": originals[INSTALLED][1]}
                if helper_changed:
                    copies["stop.py"] = old_helper[1]
                for name, content in copies.items():
                    atomic(backup / name, content, 0o600, None)
                hashes = {name: hashlib.sha256(content).hexdigest() for name, content in copies.items()}
                atomic(backup / "hashes.json", json.dumps(hashes, sort_keys=True).encode("ascii"), 0o600, None)
                directory = os.open(BACKUP_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, directory)
                os.fsync(directory)  # Persist mkdtemp's entry before publishing any repair.
                if helper_changed:
                    effective_stop()
                    require(running() == pid)
                require(all(snapshot(path) == before for path, before in originals.items()))
                changes = [(HELPER, helper, 0o600)]
                if not helper_changed:
                    changes += [(STAGED, unit, 0o600), (INSTALLED, unit, 0o644)]
                for path, content, mode in changes:
                    atomic(path, content, mode, originals[path])
                published = {path: snapshot(path) for path in originals}
                require(published[HELPER][1] == helper)
                if helper_changed:
                    # Preserve even comments, whitespace, permissions and inode identity.
                    require(all(published[path] == originals[path] for path in (STAGED, INSTALLED)))
                else:
                    require(published[STAGED][1] == published[INSTALLED][1] == unit)
                    systemctl("daemon-reload")
                effective_stop()
                require(running() == pid)
                require(all(snapshot(path) == before for path, before in published.items()))
                result["applied"] = True
            if not result["applied"]:
                require(all(snapshot(path) == before for path, before in originals.items()))
            result.update(ok=True, already_installed=already)
    except (Exception, KeyboardInterrupt):
        result.update(ok=False, applied=False, already_installed=False)  # Never expose diagnostics.
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())