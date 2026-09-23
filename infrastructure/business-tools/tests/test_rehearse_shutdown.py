"""Offline synthetic-shutdown exception checks; never contact Docker/systemd/cloud."""

import ast
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rehearse_shutdown", ROOT / "scripts/rehearse-shutdown.py")
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)
SOURCE = (ROOT / "runtime/stop.py").read_text()
IMAGE = "caddy:2.10.2@sha256:" + "0123456789abcdef" * 4
PROJECT = "bt-shutdown-" + "a" * 32
PRIVATE = "synthetic-private-diagnostic"


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(rehearsal.subprocess, "run", side_effect=AssertionError("host call")))
        self.stack.enter_context(patch.object(rehearsal.ctypes, "CDLL", side_effect=AssertionError("bus call")))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.out))
        self.stack.enter_context(redirect_stderr(self.err))

    def test_preview_has_only_fresh_names_counts_and_no_reads_writes_or_commands(self):
        with patch.object(rehearsal, "command") as command, \
                patch.object(rehearsal, "helper_source") as source, \
                patch.object(rehearsal.tempfile, "mkdtemp") as mkdir:
            self.assertEqual(rehearsal.main(["--image", IMAGE]), 0)
        command.assert_not_called()
        source.assert_not_called()
        mkdir.assert_not_called()
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 6)
        projects = set()
        scenarios = set()
        for line in lines:
            fields = dict(token.split("=", 1) for token in line.split())
            self.assertEqual(set(fields), {"phase", "scenario", "project", "unit", "containers"})
            self.assertEqual(fields["phase"], "preview")
            self.assertRegex(fields["project"], r"^bt-shutdown-[0-9a-f]{32}$")
            self.assertEqual(fields["unit"], fields["project"] + ".service")
            self.assertEqual(fields["containers"], "2")
            projects.add(fields["project"])
            scenarios.add(fields["scenario"])
        self.assertEqual(len(projects), 6)
        self.assertEqual(scenarios, {"strict-clean-native-caddy-1", "strict-clean-native-caddy-2",
            "strict-clean-both-zero", "expected-nonzero-caddy-term-7",
            "expected-nonzero-spontaneous-downstream-7", "expected-nonzero-early-downstream-7"})
        self.assertNotIn(IMAGE, self.out.getvalue())

    def test_invalid_cli_never_echoes_input_and_requires_a_caddy_digest(self):
        for args in ([], ["--image", PRIVATE], ["--image", IMAGE, "--unknown", PRIVATE],
                     ["--image", "caddy:latest"], ["--image", "other@sha256:" + "a" * 64]):
            with self.subTest(args=args):
                self.assertEqual(rehearsal.main(args), 1)
        self.assertNotIn(PRIVATE, self.out.getvalue() + self.err.getvalue())

    def test_run_preflight_pins_version_rejects_volumes_and_runs_distinct_exit_cases(self):
        info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
        volumes_json = '{"/config":{},"/data":{}}'
        for version, volumes, expected in (("2.40.3", volumes_json, 0),
                ("2.40.3+ds1-0ubuntu1~24.04.1", "null", 0),
                ("2.40.3+ds1-0ubuntu1~24.04.1", volumes_json, 0),
                ("2.40.2", volumes_json, 1), ("2.40.3", '{"/other":{}}', 1)):
            with self.subTest(version=version, volumes=volumes), \
                    patch.object(rehearsal.sys, "platform", "linux"), \
                    patch.object(rehearsal.os, "geteuid", return_value=0), \
                    patch.object(rehearsal.os, "umask"), \
                    patch.object(Path, "read_text", return_value="ID=ubuntu\n"), \
                    patch.object(Path, "lstat", return_value=info), \
                    patch.object(rehearsal, "helper_source", return_value=SOURCE), \
                    patch.object(rehearsal.tempfile, "mkdtemp", return_value="/var/tmp/bt-shutdown-test") as mkdir, \
                    patch.object(rehearsal, "private"), \
                    patch.object(rehearsal, "command", side_effect=[SimpleNamespace(stdout=version), SimpleNamespace(stdout=volumes)]) as command, \
                    patch.object(rehearsal, "rehearse", return_value=True) as run:
                self.assertEqual(rehearsal.main(["--run", "--image", IMAGE, "--baseline"]), expected)
                mkdir.assert_called_once_with(prefix="bt-shutdown-", dir="/var/tmp")
                if expected:
                    run.assert_not_called()
                else:
                    self.assertEqual([call.args[3] for call in run.call_args_list],
                                     [*rehearsal.CASES, "baseline-observation"])
                    self.assertEqual([call.kwargs for call in run.call_args_list], [
                        {}, {}, {"term_status": 0, "caddy_status": 0}, {"caddy_status": 7},
                        {"spontaneous": True}, {"caddy_status": 0, "early_failure": True},
                        {"term_status": 0, "caddy_status": 0, "baseline": True}])
                    self.assertEqual(command.call_args.args[0][-5:], ["image", "inspect", "--format", "{{json .Config.Volumes}}", IMAGE])

    def test_run_requires_root_before_host_reads_or_commands(self):
        with patch.object(rehearsal.sys, "platform", "linux"), \
                patch.object(rehearsal.os, "geteuid", return_value=1000), \
                patch.object(rehearsal, "helper_source") as source:
            self.assertEqual(rehearsal.main(["--run", "--image", IMAGE]), 1)
        source.assert_not_called()

    def test_helper_requires_regular_root_owned_0600_and_no_symlink(self):
        helper = self.directory / "stop.py"
        rehearsal.private(helper, SOURCE)
        real = os.stat(helper)
        for uid, mode, allowed in ((0, 0o600, True), (1, 0o600, False), (0, 0o644, False)):
            info = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFREG | mode, st_size=real.st_size)
            with patch.object(rehearsal.os, "fstat", return_value=info):
                if allowed:
                    self.assertEqual(rehearsal.helper_source(helper), SOURCE)
                else:
                    with self.assertRaises(ValueError):
                        rehearsal.helper_source(helper)
        link = self.directory / "link.py"
        link.symlink_to(helper)
        with self.assertRaises(ValueError):
            rehearsal.helper_source(link)
        with self.assertRaises(ValueError):
            rehearsal.helper_source(Path("relative.py"))

    def test_adaptation_changes_only_four_real_helper_constants(self):
        wrapper = self.directory / (PROJECT + ".sh")
        changed = rehearsal.adapt(SOURCE, PROJECT + ".service", wrapper, self.directory, PROJECT)
        before, after = ast.parse(SOURCE), ast.parse(changed)
        different = []
        for old, new in zip(before.body, after.body, strict=True):
            if ast.dump(old) != ast.dump(new):
                self.assertIsInstance(old, ast.Assign)
                different.append(old.targets[0].id)
        self.assertEqual(different, ["UNIT_CGROUP", "CGROUP_PROCS", "COMPOSE", "CONTAINERS"])
        namespace = {"__name__": "not_main"}
        exec(compile(changed, "fixture", "exec"), namespace)
        self.assertEqual(namespace["UNIT_CGROUP"], "/system.slice/" + PROJECT + ".service")
        self.assertEqual(namespace["COMPOSE"], str(wrapper))
        self.assertEqual(namespace["CONTAINERS"][:5], tuple(rehearsal.docker(self.directory)))
        self.assertEqual(namespace["CONTAINERS"][-1], "label=com.docker.compose.project=" + PROJECT)
        with self.assertRaises(ValueError):
            rehearsal.adapt(SOURCE.replace('COMPOSE = ', 'COMPOSE='), "unit", wrapper, self.directory, PROJECT)

    def test_named_synthetic_shutdown_exception_is_fully_isolated(self):
        for options in rehearsal.CASES.values():
            model = rehearsal.fixture(IMAGE, **options)
            self.assertEqual(set(model), {"services"})
            self.assertEqual(set(model["services"]), {"caddy", "downstream"})
            for service in model["services"].values():
                self.assertFalse({"volumes", "ports", "environment", "env_file", "networks", "build",
                                  "privileged", "user"} & service.keys())
                self.assertEqual(service["image"], IMAGE)
                self.assertEqual(service["tmpfs"], ["/config:size=1m", "/data:size=1m", "/tmp:size=1m"])
                for key, value in {"network_mode": "none", "read_only": True, "cap_drop": ["ALL"],
                                   "security_opt": ["no-new-privileges:true"], "mem_limit": "64m",
                                   "pids_limit": 32, "logging": {"driver": "none"},
                                   "pull_policy": "never", "stop_grace_period": "15s",
                                   "stop_signal": "SIGTERM", "healthcheck": {"disable": True}}.items():
                    self.assertEqual(service[key], value)
            downstream = model["services"]["downstream"]
            self.assertEqual(downstream["entrypoint"], ["/bin/sh"])
            self.assertIn(f"trap 'exit {options.get('term_status', 143)}' TERM", downstream["command"][1])
            self.assertIn("wait $$!", downstream["command"][1])

    def test_clean_native_caddy_inherits_image_process_and_capability(self):
        for name in ("strict-clean-native-caddy-1", "strict-clean-native-caddy-2"):
            model = rehearsal.fixture(IMAGE, **rehearsal.CASES[name])
            caddy = model["services"]["caddy"]
            self.assertFalse({"entrypoint", "command", "user", "configs"} & caddy.keys())
            self.assertNotIn("sysctls", caddy)
            self.assertEqual(caddy["cap_add"], ["NET_BIND_SERVICE"])
            self.assertNotIn("cap_add", model["services"]["downstream"])
            self.assertEqual(model["services"]["downstream"]["command"][1],
                             "trap 'exit 143' TERM; while :; do sleep 1 & wait $$!; done")

    def test_both_zero_control_and_caddy_term_seven_use_explicit_shell_ingress(self):
        for name, status in (("strict-clean-both-zero", 0), ("expected-nonzero-caddy-term-7", 7)):
            model = rehearsal.fixture(IMAGE, **rehearsal.CASES[name])
            caddy = model["services"]["caddy"]
            self.assertEqual(caddy["entrypoint"], ["/bin/sh"])
            self.assertNotIn("sysctls", caddy)
            self.assertNotIn("cap_add", caddy)
            self.assertEqual(caddy["command"][1],
                             f"trap 'exit {status}' TERM; while :; do sleep 1 & wait $$!; done")

    def test_spontaneous_fixture_exits_in_normal_control_flow_not_a_term_trap(self):
        requested = rehearsal.fixture(IMAGE, 143)
        spontaneous = rehearsal.fixture(IMAGE, 143, spontaneous=True)
        self.assertEqual(spontaneous["services"]["caddy"], requested["services"]["caddy"])
        downstream = spontaneous["services"]["downstream"]["command"][1]
        self.assertEqual(downstream, "trap 'exit 143' TERM; sleep 10 & wait $$!; exit 7")
        self.assertNotIn("exit 7", requested["services"]["downstream"]["command"][1])

    def test_early_downstream_failure_occurs_before_delayed_ingress_exits_zero(self):
        model = rehearsal.fixture(IMAGE, **rehearsal.CASES["expected-nonzero-early-downstream-7"])
        self.assertEqual(model["services"]["caddy"]["command"][1],
                         "trap 'sleep 10; exit 0' TERM; while :; do sleep 1 & wait $$!; done")
        self.assertEqual(model["services"]["downstream"]["command"][1],
                         "trap 'exit 143' TERM; sleep 5 & wait $$!; exit 7")

    def scenario(self, term_status=143, observed=0, baseline=False, counts=None, fault=None,
                 spontaneous=False, states=None, stop_return=0, caddy_status=None,
                 early_failure=False, observations=None):
        self.calls = []
        if early_failure and observations is None:
            observations = [("active", "success", 0),
                            ("failed" if observed else "inactive", "exit-code" if observed else "success", observed)]

        def command(args, directory, **kwargs):
            self.calls.append((args, kwargs))
            if fault:
                fault(args)
            output = ""
            if "--property=LoadState" in args:
                output = "not-found\n"
            elif "--value" in args:
                output = "failed\n"
            elif "--property=Result" in args:
                if observations:
                    state, result, status = observations.pop(0)
                else:
                    state = states.pop(0) if states else ('failed' if observed else 'inactive')
                    result, status = ('exit-code' if observed else 'success'), observed
                output = f"ActiveState={state}\nResult={result}\nExecMainStatus={status}\n"
            status = stop_return if args[:2] == ["/usr/bin/systemctl", "stop"] else 0
            return subprocess.CompletedProcess(args, status, output, PRIVATE)

        if counts is None:
            counts = [0, 2, 2, 0, 0] if spontaneous else [0, 2, 0, 0]
        with patch.object(rehearsal, "command", side_effect=command), \
                patch.object(rehearsal, "inventory", side_effect=counts) as inventory, \
                patch.object(rehearsal, "reference", return_value=nullcontext()), \
                patch.object(rehearsal.time, "sleep"):
            name = "expected-nonzero-spontaneous-downstream-7" if spontaneous else "requested-stop"
            result = rehearsal.rehearse(self.directory, SOURCE, IMAGE, name, PROJECT,
                                        term_status, baseline, spontaneous, caddy_status, early_failure)
        self.inventory_calls = inventory.call_args_list
        self.assertNotIn(PRIVATE, self.out.getvalue() + self.err.getvalue())
        return result

    def test_clean_requires_inactive_success_zero_and_no_containers_before_cleanup(self):
        self.assertTrue(self.scenario())
        launch = next(args for args, _ in self.calls if args[0] == "/usr/bin/systemd-run")
        for value in ("--property=Type=simple", "--property=Restart=no", "--property=TimeoutStopSec=120",
                      "--property=StandardOutput=null", "--property=StandardError=null"):
            self.assertIn(value, launch)
        self.assertIn("--property=ExecStop=/usr/bin/python3 " + str(self.directory / (PROJECT + ".py")), launch)
        self.assertEqual(launch[-4:], ["up", "--abort-on-container-exit", "--pull", "never"])
        self.assertNotIn("--collect", launch)
        for forbidden in ("SuccessExitStatus", "KillSignal", "KillMode", "RestartForceExitStatus",
                          "RestartPreventExitStatus", "ExecStopPost", "--exit-code-from", "--timeout"):
            self.assertFalse(any(forbidden in arg for arg in launch))
        wrapper = self.directory / (PROJECT + ".sh")
        text = wrapper.read_text()
        for token in ("exec /usr/bin/env -i", "--project-name " + PROJECT, "--env-file ", "--file ", '"$@"'):
            self.assertIn(token, text)
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / (PROJECT + ".py")).stat().st_mode), 0o600)
        self.assertEqual(json.loads((self.directory / (PROJECT + ".result.json")).read_text())["exit"], 0)
        self.assertEqual(sum(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls), 1)
        self.assertEqual(self.calls[-1][0], [str(wrapper), "down", "--remove-orphans"])
        self.assertNotIn("reset-failed", str(self.calls))
        self.assertNotIn("--volumes", str(self.calls))

    def test_requested_stop_with_term_exit_143_requires_successful_main_exit(self):
        self.assertTrue(self.scenario(term_status=143))
        model = json.loads((self.directory / (PROJECT + ".json")).read_text())
        self.assertEqual(model, rehearsal.fixture(IMAGE, 143))
        self.assertEqual(sum(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls), 1)

    def test_both_zero_shell_control_requires_successful_main_exit(self):
        self.assertTrue(self.scenario(term_status=0, caddy_status=0))

    def test_requested_caddy_term_exit_seven_preserves_genuine_main_failure(self):
        self.assertTrue(self.scenario(caddy_status=7, observed=7))
        record = json.loads((self.directory / (PROJECT + ".result.json")).read_text())
        self.assertEqual((record["state"], record["result"], record["exit"]), ("failed", "exit-code", 7))
        self.assertEqual(record["containers"], 0)
        self.assertEqual(sum(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls), 1)

    def test_requested_caddy_failure_is_not_allowed_to_become_success(self):
        self.assertFalse(self.scenario(caddy_status=7))

    def test_requested_caddy_failure_rejects_wrong_nonzero_exit(self):
        self.assertFalse(self.scenario(caddy_status=7, observed=130))

    def test_early_downstream_failure_during_ingress_drain_stays_failed(self):
        self.assertTrue(self.scenario(caddy_status=0, early_failure=True, observed=7))
        self.assertEqual(sum(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls), 1)

    def test_early_downstream_failure_must_not_become_success(self):
        self.assertFalse(self.scenario(caddy_status=0, early_failure=True))

    def test_early_case_rejects_already_failed_unit_without_stopping_it_again(self):
        self.assertFalse(self.scenario(caddy_status=0, early_failure=True, observed=7,
                                      counts=[0, 2, 0], observations=[("failed", "exit-code", 7)]))
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls))

    def test_requested_stop_rejects_propagated_container_exit_143(self):
        self.assertFalse(self.scenario(term_status=143, observed=143))

    def test_requested_stop_rejects_compose_cancellation_exit_130(self):
        self.assertFalse(self.scenario(term_status=143, observed=130))

    def test_requested_term_exit_seven_is_not_a_spontaneous_failure_test(self):
        self.assertFalse(self.scenario(term_status=7, observed=7))

    def test_requested_stop_command_must_succeed_even_if_unit_looks_successful(self):
        self.assertFalse(self.scenario(stop_return=1))

    def test_spontaneous_exit_seven_waits_for_failed_unit_without_systemctl_stop(self):
        self.assertTrue(self.scenario(term_status=143, spontaneous=True, observed=7,
                                      states=["active", "deactivating", "failed"]))
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls))
        record = json.loads((self.directory / (PROJECT + ".result.json")).read_text())
        self.assertIsNone(record["stop_exit"])
        self.assertEqual((record["state"], record["result"], record["exit"]), ("failed", "exit-code", 7))
        self.assertEqual(record["scenario"], "expected-nonzero-spontaneous-downstream-7")
        self.assertEqual((record["containers"], record["running_containers"]), (2, 0))
        self.assertIn(json.dumps(record), self.out.getvalue())
        self.assertEqual(len(self.inventory_calls), 5)
        self.assertEqual(self.inventory_calls[3].args, (self.directory, PROJECT))
        self.assertEqual(self.inventory_calls[3].kwargs, {"running": True})
        self.assertEqual(self.inventory_calls[4].args, (self.directory, PROJECT))
        self.assertEqual(self.inventory_calls[4].kwargs, {})
        launch = next(args for args, _ in self.calls if args[0] == "/usr/bin/systemd-run")
        self.assertIn("--property=Restart=no", launch)
        self.assertEqual(self.calls[-1][0], [str(self.directory / (PROJECT + ".sh")), "down", "--remove-orphans"])
        self.assertIn("phase=cleanup scenario=" + record["scenario"] + " containers=0", self.out.getvalue())
        self.assertNotIn("reset-failed", str(self.calls))

    def test_wrong_exit_is_failure_even_after_cleanup_succeeds(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=0))

    def test_spontaneous_failure_rejects_wrong_nonzero_exit(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=130))

    def test_spontaneous_failure_requires_failed_state(self):
        self.assertFalse(self.scenario(spontaneous=True, observations=[("inactive", "exit-code", 7)]))

    def test_failure_requires_exact_state_result_and_exit_not_just_nonzero(self):
        self.assertFalse(self.scenario(spontaneous=True, observations=[("failed", "signal", 7)]))

    def test_clean_rejects_allowed_but_unsuccessful_result(self):
        self.assertFalse(self.scenario(observations=[("inactive", "oom-kill", 0)]))

    def test_spontaneous_wait_is_bounded_and_does_not_stop_a_failed_unit(self):
        with patch.object(rehearsal.time, "monotonic", side_effect=[0, 0, 0, 0, 0, 121]):
            self.assertFalse(self.scenario(spontaneous=True, states=["active"], counts=[0, 2, 0]))
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls))
        self.assertEqual(self.calls[-1][0][1:], ["down", "--remove-orphans"])

    def test_leftover_container_is_failure_even_after_cleanup_succeeds(self):
        self.assertFalse(self.scenario(counts=[0, 2, 1, 0]))

    def test_spontaneous_failure_rejects_partial_fixture_before_cleanup(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=7, counts=[0, 2, 1, 0, 0]))
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls))

    def test_spontaneous_failure_rejects_removed_fixture_before_cleanup(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=7, counts=[0, 2, 0, 0, 0]))

    def test_spontaneous_failure_rejects_extra_fixture_container_before_cleanup(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=7, counts=[0, 2, 3, 0, 0]))

    def test_spontaneous_failure_rejects_running_peer_even_after_cleanup_succeeds(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=7, counts=[0, 2, 2, 1, 0]))
        record = json.loads((self.directory / (PROJECT + ".result.json")).read_text())
        self.assertEqual((record["containers"], record["running_containers"]), (2, 1))

    def test_spontaneous_failure_requires_no_containers_after_final_cleanup(self):
        self.assertFalse(self.scenario(spontaneous=True, observed=7, counts=[0, 2, 2, 0, 1]))
        self.assertIn("phase=cleanup scenario=expected-nonzero-spontaneous-downstream-7 containers=1",
                      self.out.getvalue())

    def test_spontaneous_failure_cannot_pass_when_final_cleanup_fails(self):
        def fail(args):
            if args[1:] == ["down", "--remove-orphans"]:
                raise RuntimeError(PRIVATE)
        self.assertFalse(self.scenario(spontaneous=True, observed=7, counts=[0, 2, 2, 0], fault=fail))
        self.assertIn("phase=cleanup-failed", self.out.getvalue())
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls))

    def test_baseline_records_failure_without_requiring_or_rejecting_panic(self):
        self.assertTrue(self.scenario(observed=2, baseline=True))
        launch = next(args for args, _ in self.calls if args[0] == "/usr/bin/systemd-run")
        self.assertIn("--property=ExecStop=" + str(self.directory / (PROJECT + ".sh")) + " down", launch)

    def test_baseline_also_accepts_clean_observation(self):
        self.assertTrue(self.scenario(baseline=True))

    def test_readiness_is_bounded_and_failed_unit_is_not_stopped_again(self):
        with patch.object(rehearsal.time, "monotonic", side_effect=[0, 0, 30, 31]):
            self.assertFalse(self.scenario(counts=[0, 0, 0]))
        self.assertFalse(any(args[:2] == ["/usr/bin/systemctl", "stop"] for args, _ in self.calls))
        self.assertEqual(self.calls[-1][0][1:], ["down", "--remove-orphans"])

    def test_launch_exception_is_quiet_and_cleanup_is_project_scoped(self):
        def fail(args):
            if args[0] == "/usr/bin/systemd-run":
                raise RuntimeError(PRIVATE)
        self.assertFalse(self.scenario(counts=[0, 0], fault=fail))
        self.assertEqual(self.calls[-1][0], [str(self.directory / (PROJECT + ".sh")), "down", "--remove-orphans"])

    def test_cleanup_down_is_still_attempted_if_stop_state_lookup_fails(self):
        def fail(args):
            if args[0] == "/usr/bin/systemd-run" or "--property=ActiveState" in args:
                raise RuntimeError(PRIVATE)
        self.assertFalse(self.scenario(counts=[0, 0], fault=fail))
        self.assertEqual(self.calls[-1][0][1:], ["down", "--remove-orphans"])

    def test_collision_never_starts_stops_or_cleans_existing_project(self):
        with self.assertRaises(ValueError):
            self.scenario(counts=[1])
        self.assertEqual(self.calls, [])

    def test_subprocesses_capture_all_output_and_ignore_host_environment(self):
        with patch.dict(os.environ, {"DOCKER_HOST": PRIVATE, "COMPOSE_FILE": PRIVATE}), \
                patch.object(rehearsal.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", PRIVATE)) as run:
            rehearsal.command(["synthetic"], self.directory)
        self.assertEqual(run.call_args.kwargs, {"cwd": self.directory, "env": {"PATH": rehearsal.PATH},
            "stdin": subprocess.DEVNULL, "capture_output": True, "text": True, "timeout": 10, "close_fds": True})

    def test_observation_accepts_only_safe_enums_and_bounded_exit_codes(self):
        for result in ("success", "exit-code", "signal", "core-dump", "timeout", "resources",
                       "protocol", "start-limit-hit", "watchdog", "exec-condition", "oom-kill"):
            output = f"ActiveState=failed\nResult={result}\nExecMainStatus=7\n"
            with patch.object(rehearsal, "command", return_value=SimpleNamespace(stdout=output)):
                self.assertEqual(rehearsal.observe(self.directory, PROJECT + ".service"),
                                 {"state": "failed", "result": result, "exit": 7})
        for output in (f"ActiveState={PRIVATE}\nResult=success\nExecMainStatus=0\n",
                       f"ActiveState=inactive\nResult={PRIVATE}\nExecMainStatus=0\n",
                       "ActiveState=inactive\nResult=success\nExecMainStatus=256\n",
                       "ActiveState=inactive\nResult=success\nExecMainStatus=-1\n",
                       "ActiveState=inactive\nResult=success\nExecMainStatus=７\n",
                       "ActiveState=inactive\nResult=success\nResult=success\n",
                       "ActiveState=inactive\nResult=success\nExecMainStatus=0\nExtra=value\n"):
            with patch.object(rehearsal, "command", return_value=SimpleNamespace(stdout=output)):
                with self.assertRaises(ValueError):
                    rehearsal.observe(self.directory, PROJECT + ".service")
        self.assertNotIn(PRIVATE, self.out.getvalue() + self.err.getvalue())

    def test_bus_reference_uses_fixed_local_socket_and_releases_on_error(self):
        lib = Mock()
        for name in ("sd_bus_new", "sd_bus_set_address", "sd_bus_set_bus_client", "sd_bus_start", "sd_bus_call_method"):
            getattr(lib, name).return_value = 0
        with patch.object(rehearsal.ctypes, "CDLL", return_value=lib):
            with self.assertRaises(RuntimeError), rehearsal.reference(PROJECT + ".service"):
                raise RuntimeError(PRIVATE)
        self.assertEqual(lib.sd_bus_set_address.call_args.args[1], b"unix:path=/run/dbus/system_bus_socket")
        self.assertEqual(lib.sd_bus_call_method.call_args.args[4], b"RefUnit")
        lib.sd_bus_flush_close_unref.assert_called_once()


if __name__ == "__main__":
    unittest.main()