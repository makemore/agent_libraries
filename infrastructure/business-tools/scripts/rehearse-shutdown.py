#!/usr/bin/env python3
"""Preview default; --run requires root Ubuntu, Compose 2.40.3, local Caddy digest.
Stage public runtime/stop.py beside this script or use --helper (root-owned 0600).
Named synthetic-shutdown exception: network-none, read-only caddy/downstream,
15s grace (tests/test_rehearse_shutdown.py), never production policy/data.
Strict-clean cases use native Caddy with downstream TERM 143, plus both-zero
shell controls. Expected-nonzero cases use Caddy TERM 7, autonomous downstream
exit 7, and downstream exit 7 while shell ingress drains. --baseline adds
direct-down observations, not a panic assertion. Exit 130/143 is NOT clean.
Named expected-nonzero-spontaneous-downstream-7 fixture: production stops the
whole project on container exit, but does not guarantee removal before automatic
restart. Transient Restart=no preserves the actual failure for inspection; no
ExecStop invocation is claimed. Only this fixture requires two stopped containers
before explicit final cleanup, which must remove all (tests/test_rehearse_shutdown.py).
Protected /var/tmp evidence and failed units remain; no reset-failed.
No pulls. This proves neither application shutdown nor durability.
"""

import argparse
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid

PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
IMAGE = r"(?:docker.io/library/)?caddy(?::[A-Za-z0-9_.-]+)?@sha256:[a-f0-9]{64}"
CASES = {
    "strict-clean-native-caddy-1": {},
    "strict-clean-native-caddy-2": {},
    "strict-clean-both-zero": {"term_status": 0, "caddy_status": 0},
    "expected-nonzero-caddy-term-7": {"caddy_status": 7},
    "expected-nonzero-spontaneous-downstream-7": {"spontaneous": True},
    "expected-nonzero-early-downstream-7": {"caddy_status": 0, "early_failure": True},
}


def require(condition):
    if not condition:
        raise ValueError("Rehearsal rejected")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid arguments")  # Never echo an argument or diagnostic.


def command(args, directory, *, timeout=10, check=True):
    result = subprocess.run(args, cwd=directory, env={"PATH": PATH},
                            stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout, close_fds=True)
    require(not check or result.returncode == 0)
    return result


def private(path, content, mode=0o600):
    with path.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)


def helper_source(path):
    require(path.is_absolute() and path.resolve(strict=True) == path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size < 65536)
        return stream.read()


def docker(directory):
    return ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock",
            "--config", str(directory)]  # Never read root's Docker config/context.


def inventory(directory, project, *, running=False, timeout=10):
    args = docker(directory) + ["ps", "--all", "--quiet", "--filter",
                               "label=com.docker.compose.project=" + project]
    if running:
        args += ["--filter", "status=running"]
    ids = command(args, directory, timeout=timeout).stdout.splitlines()
    require(all(re.fullmatch(r"[a-f0-9]{12,64}", value) for value in ids))
    return len(ids)


def adapt(source, unit, wrapper, directory, project):
    replacements = {
        'UNIT_CGROUP = "/system.slice/business-tools.service"':
            f'UNIT_CGROUP = "/system.slice/{unit}"',
        'CGROUP_PROCS = Path("/sys/fs/cgroup/system.slice/business-tools.service/cgroup.procs")':
            f'CGROUP_PROCS = Path("/sys/fs/cgroup/system.slice/{unit}/cgroup.procs")',
        'COMPOSE = "/opt/business-tools/compose.sh"': f"COMPOSE = {str(wrapper)!r}",
        'CONTAINERS = ("/usr/bin/docker", "ps", "--all", "--quiet", "--filter",\n'
        '              "label=com.docker.compose.project=business-tools")':
            "CONTAINERS = " + repr(tuple(docker(directory) + [
                "ps", "--all", "--quiet", "--filter",
                "label=com.docker.compose.project=" + project])),
    }
    for old, new in replacements.items():
        require(source.count(old) == 1)
        source = source.replace(old, new)
    compile(source, "adapted-stop.py", "exec")
    return source


def fixture(image, term_status=143, spontaneous=False, caddy_status=None, early_failure=False):
    service = {"image": image, "pull_policy": "never",
               "network_mode": "none", "read_only": True, "cap_drop": ["ALL"],
               "tmpfs": ["/config:size=1m", "/data:size=1m", "/tmp:size=1m"],
               "security_opt": ["no-new-privileges:true"], "mem_limit": "64m",
               "pids_limit": 32, "logging": {"driver": "none"},
               "stop_grace_period": "15s", "stop_signal": "SIGTERM",
               "healthcheck": {"disable": True}}
    services = {"caddy": dict(service),
                "downstream": dict(service, entrypoint=["/bin/sh"], command=["-c",
                    f"trap 'exit {term_status}' TERM; while :; do sleep 1 & wait $$!; done"])}
    if caddy_status is None:
        # Match production's capability: the pinned binary has a file capability
        # and exec itself fails EPERM if NET_BIND_SERVICE is outside the bounding
        # set. No process/user/config override or host networking/ports.
        # Covered by test_clean_native_caddy_inherits_image_process_and_capability.
        services["caddy"]["cap_add"] = ["NET_BIND_SERVICE"]
    else:
        drain = "sleep 10; " if early_failure else ""
        services["caddy"].update(entrypoint=["/bin/sh"], command=["-c",
            f"trap '{drain}exit {caddy_status}' TERM; while :; do sleep 1 & wait $$!; done"])
    if spontaneous or early_failure:
        # No stop/kill/exec trigger: PID 1 exits from its own normal control flow.
        # Early failure races a 5s lifetime against a 10s ingress TERM drain;
        # the requested stop must start while both containers are still running.
        delay = 5 if early_failure else 10
        services["downstream"]["command"] = ["-c",
            f"trap 'exit {term_status}' TERM; sleep {delay} & wait $$!; exit 7"]
    return {"services": services}


@contextmanager
def reference(unit):
    """Keep successful transient results inspectable, without altering unit behavior."""
    lib = ctypes.CDLL("libsystemd.so.0")
    ptr, text = ctypes.c_void_p, ctypes.c_char_p
    for name, arguments, result in (
        ("sd_bus_new", [ctypes.POINTER(ptr)], ctypes.c_int),
        ("sd_bus_set_address", [ptr, text], ctypes.c_int),
        ("sd_bus_set_bus_client", [ptr, ctypes.c_int], ctypes.c_int),
        ("sd_bus_start", [ptr], ctypes.c_int),
        ("sd_bus_call_method", [ptr, text, text, text, text, ptr, ptr, text], ctypes.c_int),
        ("sd_bus_flush_close_unref", [ptr], ptr),
    ):
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    bus = ptr()
    try:
        require(lib.sd_bus_new(ctypes.byref(bus)) >= 0)
        require(lib.sd_bus_set_address(bus, b"unix:path=/run/dbus/system_bus_socket") >= 0)
        require(lib.sd_bus_set_bus_client(bus, 1) >= 0)
        require(lib.sd_bus_start(bus) >= 0)
        require(lib.sd_bus_call_method(bus, b"org.freedesktop.systemd1",
                b"/org/freedesktop/systemd1", b"org.freedesktop.systemd1.Manager",
                b"RefUnit", None, None, b"s", text(unit.encode("ascii"))) >= 0)
        yield
    finally:
        lib.sd_bus_flush_close_unref(bus)  # Disconnect releases ONLY our reference.


def observe(directory, unit, *, timeout=10):
    output = command(["/usr/bin/systemctl", "show", unit, "--property=ActiveState",
                      "--property=Result", "--property=ExecMainStatus"], directory,
                     timeout=timeout).stdout
    lines = output.splitlines()
    require(len(lines) == 3)
    fields = dict(line.split("=", 1) for line in lines)
    require(set(fields) == {"ActiveState", "Result", "ExecMainStatus"})
    state, result = fields["ActiveState"], fields["Result"]
    require(state in {"inactive", "failed", "active", "activating", "deactivating",
                      "reloading", "refreshing", "maintenance"})
    require(result in {"success", "exit-code", "signal", "core-dump", "timeout",
                       "resources", "protocol", "start-limit-hit", "watchdog",
                       "exec-condition", "oom-kill"})
    require(re.fullmatch(r"[0-9]{1,3}", fields["ExecMainStatus"]) is not None)
    require(int(fields["ExecMainStatus"]) <= 255)
    return {"state": state, "result": result, "exit": int(fields["ExecMainStatus"])}


def rehearse(directory, source, image, name, project, term_status=143, baseline=False,
             spontaneous=False, caddy_status=None, early_failure=False):
    require(re.fullmatch(r"bt-shutdown-[a-f0-9]{32}", project) is not None)
    unit, wrapper = project + ".service", directory / (project + ".sh")
    helper, model = directory / (project + ".py"), directory / (project + ".json")
    private(model, json.dumps(fixture(image, term_status, spontaneous, caddy_status, early_failure)))
    private(helper, adapt(source, unit, wrapper, directory, project))
    compose = docker(directory) + ["compose", "--project-name", project,
        "--project-directory", str(directory), "--env-file", str(directory / "empty.env"),
        "--file", str(model)]
    private(wrapper, "#!/bin/sh\nexec /usr/bin/env -i " + shlex.join([
        "PATH=" + PATH, "HOME=" + str(directory), *compose]) + ' "$@"\n', 0o700)
    require(inventory(directory, project) == 0)
    require(command(["/usr/bin/systemctl", "show", unit, "--property=LoadState", "--value"],
                    directory, check=False).stdout.strip() == "not-found")
    stop_attempted, passed = False, False
    try:
        stop = f"{wrapper} down" if baseline else f"/usr/bin/python3 {helper}"
        command(["/usr/bin/systemd-run", "--unit", unit, "--property=Type=simple",
            "--property=Restart=no", "--property=TimeoutStopSec=120", "--property=Slice=system.slice",
            "--property=StandardOutput=null", "--property=StandardError=null",
            "--property=ExecStop=" + stop, str(wrapper), "up", "--abort-on-container-exit",
            "--pull", "never"], directory)
        deadline = time.monotonic() + 30
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0)
            if inventory(directory, project, running=True, timeout=min(5, remaining)) == 2:
                break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        with reference(unit):
            stop_exit = None
            if spontaneous:
                # Observe spontaneous termination, not evidence that ExecStop ran.
                # Never systemctl stop: that would request shutdown or clear failure.
                deadline = time.monotonic() + 120
                while True:
                    remaining = deadline - time.monotonic()
                    require(remaining > 0)
                    observed = observe(directory, unit, timeout=min(5, remaining))
                    if observed["state"] in {"inactive", "failed"}:
                        break
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                stop_attempted = True  # Already terminal; preserve that result.
            else:
                if early_failure:
                    # Reject a failure that happened before the requested stop;
                    # it is not evidence for the ingress-draining race case.
                    require(observe(directory, unit) == {"state": "active", "result": "success", "exit": 0})
                stop_attempted = True
                stopped = command(["/usr/bin/systemctl", "stop", unit], directory,
                                  timeout=150, check=False)
                stop_exit = stopped.returncode
                observed = observe(directory, unit)
            record = {"phase": "observed", "scenario": name, "project": project,
                      "unit": unit, **observed, "stop_exit": stop_exit,
                      "containers": inventory(directory, project)}
            if spontaneous:
                record["running_containers"] = inventory(directory, project, running=True)
            private(directory / (project + ".result.json"), json.dumps(record) + "\n")
            print(json.dumps(record), flush=True)
            failure = spontaneous or early_failure or caddy_status == 7
            expected = ("failed", "exit-code", 7) if failure else ("inactive", "success", 0)
            outcome_matches = (record["state"], record["result"], record["exit"]) == expected
            if spontaneous:
                passed = (outcome_matches and record["containers"] == 2
                          and record["running_containers"] == 0)
            else:
                passed = record["containers"] == 0 and (baseline or
                         (stop_exit == 0 and outcome_matches))
    except (Exception, KeyboardInterrupt):
        print(f"phase=failed scenario={name}", flush=True)
    finally:
        try:
            # Do not stop an already failed unit: preserve its recorded failure.
            if not stop_attempted:
                state = command(["/usr/bin/systemctl", "show", unit, "--property=ActiveState",
                                 "--value"], directory, check=False).stdout.strip()
                if state in {"active", "activating", "deactivating"}:
                    command(["/usr/bin/systemctl", "stop", unit], directory, timeout=150, check=False)
        except (Exception, KeyboardInterrupt):
            print(f"phase=cleanup-stop-failed scenario={name}", flush=True)
            passed = False
        try:
            command([str(wrapper), "down", "--remove-orphans"], directory, timeout=60)
            count = inventory(directory, project)
            print(f"phase=cleanup scenario={name} containers={count}", flush=True)
            passed = passed and count == 0
        except (Exception, KeyboardInterrupt):
            print(f"phase=cleanup-failed scenario={name}", flush=True)
            passed = False
    return passed


def main(argv=None):
    try:
        parser = Parser(prog="rehearse-shutdown.py", description=__doc__, allow_abbrev=False)
        parser.add_argument("--run", action="store_true")
        parser.add_argument("--baseline", action="store_true")
        parser.add_argument("--image", required=True)
        parser.add_argument("--helper", type=Path, default=Path(__file__).absolute().with_name("stop.py"))
        args = parser.parse_args(argv)
        require(re.fullmatch(IMAGE, args.image) is not None)
        cases = dict(CASES)
        if args.baseline:
            cases["baseline-observation"] = {"term_status": 0, "caddy_status": 0, "baseline": True}
        plans = [(name, "bt-shutdown-" + uuid.uuid4().hex, options) for name, options in cases.items()]
        for name, project, _ in plans:
            print(f"phase={'planned' if args.run else 'preview'} scenario={name} "
                  f"project={project} unit={project}.service containers=2", flush=True)
        if not args.run:
            return 0
        require(sys.platform == "linux" and os.geteuid() == 0)
        require("ID=ubuntu" in Path("/etc/os-release").read_text().splitlines())
        source = helper_source(args.helper)
        os.umask(0o077)
        directory = Path(tempfile.mkdtemp(prefix="bt-shutdown-", dir="/var/tmp"))
        info = directory.lstat()
        require(directory.parent == Path("/var/tmp") and stat.S_ISDIR(info.st_mode)
                and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700)
        print(f"phase=prepared directory={directory}", flush=True)
        private(directory / "empty.env", "")
        require(command(docker(directory) + ["compose", "version", "--short"], directory)
                .stdout.strip() in {"2.40.3", "v2.40.3", "2.40.3+ds1-0ubuntu1~24.04.1"})
        # Cover Caddy's two image volumes with fixture-only tmpfs, never anonymous volumes.
        volumes = json.loads(command(docker(directory) + ["image", "inspect", "--format",
                "{{json .Config.Volumes}}", args.image], directory).stdout)
        require(volumes is None or isinstance(volumes, dict) and set(volumes) <= {"/config", "/data"})
        results = [rehearse(directory, source, args.image, name, project, **options)
                   for name, project, options in plans]
        print(f"phase=complete cases={len(results)} passed={sum(results)}", flush=True)
        return 0 if all(results) else 1
    except (Exception, KeyboardInterrupt):
        print("phase=failed", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())