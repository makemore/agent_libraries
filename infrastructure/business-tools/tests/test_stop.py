"""Offline ExecStop tests: synthetic cgroups and mocked Linux/process interfaces.

No root, real pidfds, systemd, Docker, cloud or host writes are required.
"""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import select
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
SPEC = importlib.util.spec_from_file_location("business_tools_stop", RUNTIME / "stop.py")
stop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stop)
INGRESS = ("/opt/business-tools/compose.sh", "stop", "caddy")
STOP = ("/opt/business-tools/compose.sh", "stop")
DOWN = ("/opt/business-tools/compose.sh", "down", "--remove-orphans")
QUERY = ("/usr/bin/docker", "ps", "--all", "--quiet", "--filter",
         "label=com.docker.compose.project=business-tools")
PRIVATE_ERROR = "synthetic private diagnostic must not escape"


class StopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.membership = root / "self-cgroup"
        self.membership.write_text("0::/system.slice/business-tools.service\n")
        self.inventory = root / "cgroup.procs"
        self.inventory.write_text("99\n42\n41\n41\n")
        self.after_exit = "99\n"
        self.events = []
        self.command_errors = {}
        self.query_output = b""
        self.poll_events = [[(141, select.POLLIN)], [(142, select.POLLIN | select.POLLHUP)]]
        # Local exception: replace OS primitives, not production root/membership
        # gates, to exercise Linux-only behavior safely on unprivileged hosts.
        self.os = SimpleNamespace(
            geteuid=mock.Mock(return_value=0), getpid=mock.Mock(return_value=99),
            environ={"MAINPID": "41"},
            pidfd_open=mock.Mock(side_effect=self.open_pidfd),
            close=mock.Mock(side_effect=lambda fd: self.events.append(("close", fd))),
        )
        self.poller = mock.Mock()
        self.poller.poll.side_effect = self.poll
        self.capture_poller = mock.Mock()
        self.capture_poller.poll.return_value = []
        self.poll_calls = 0
        self.select = SimpleNamespace(
            poll=mock.Mock(side_effect=self.make_poller), POLLIN=select.POLLIN,
            POLLERR=select.POLLERR, POLLNVAL=select.POLLNVAL,
        )
        self.time = SimpleNamespace(monotonic=mock.Mock(return_value=0.0))
        self.run = mock.Mock(side_effect=self.command)
        self.patch("SELF_CGROUP", self.membership)
        self.patch("CGROUP_PROCS", self.inventory)
        self.patch("os", self.os)
        self.patch("select", self.select)
        self.patch("time", self.time)
        self.patch("subprocess", SimpleNamespace(
            run=self.run, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE,
        ))

    def patch(self, name, value):
        patcher = mock.patch.object(stop, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_pidfd(self, pid, flags):
        self.assertEqual(flags, 0)
        self.events.append(("open", pid))
        return pid + 100

    def poll(self, timeout):
        self.assertGreater(timeout, 0)
        self.events.append("poll")
        events = self.poll_events.pop(0)
        if not self.poll_events:
            self.inventory.write_text(self.after_exit)
        return events

    def make_poller(self):
        self.poll_calls += 1
        return self.capture_poller if self.poll_calls == 1 else self.poller

    def command(self, args, **kwargs):
        name = {INGRESS: "ingress", STOP: "stop", DOWN: "down", QUERY: "query"}[args]
        self.events.append(name)
        if name in self.command_errors:
            raise self.command_errors[name]
        return subprocess.CompletedProcess(args, 0, self.query_output if name == "query" else None)

    def main(self):
        self.poll_calls = 0
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = stop.main()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        return result

    def assert_closed(self, *fds):
        self.assertEqual(self.os.close.call_args_list, [mock.call(fd) for fd in sorted(fds)])

    def test_capture_all_stop_ingress_then_all_wait_for_both_then_down(self):
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.events, [
            ("open", 41), ("open", 42), "ingress", "stop",
            "poll", "poll", "down", "query",
            ("close", 141), ("close", 142),
        ])
        self.capture_poller.poll.assert_called_once_with(0)
        self.assertEqual(self.os.pidfd_open.call_args_list, [mock.call(41, 0), mock.call(42, 0)])
        self.poller.register.assert_has_calls([mock.call(141, select.POLLIN),
                                             mock.call(142, select.POLLIN)], any_order=True)
        self.assertEqual(self.poller.unregister.call_args_list, [mock.call(141), mock.call(142)])
        self.assert_closed(141, 142)

    def test_fixed_commands_sanitized_environment_and_private_stdio(self):
        with mock.patch.dict(self.os.environ, {"COMPOSE_PROFILES": "synthetic-profile",
                                              "DOCKER_HOST": "synthetic-host",
                                              "MAINPID": "41", "EXIT_STATUS": "2"}):
            self.assertEqual(self.main(), 0)
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [INGRESS, STOP, DOWN, QUERY])
        for call in self.run.call_args_list:
            self.assertEqual(call.kwargs, {
                "check": True, "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE if call.args[0] == QUERY else subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "close_fds": True,
                "env": {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            })

    def test_main_already_gone_still_waits_for_plugin(self):
        self.os.environ = {"EXIT_STATUS": "7", "SERVICE_RESULT": "exit-code"}
        self.inventory.write_text("99\n42\n")
        self.poll_events = [[(142, select.POLLIN)]]
        self.assertEqual(self.main(), 0)
        self.os.pidfd_open.assert_called_once_with(42, 0)
        self.assertEqual(self.os.environ["EXIT_STATUS"], "7")
        self.assertEqual(self.os.environ["SERVICE_RESULT"], "exit-code")
        self.assertEqual(self.events, [("open", 42), "ingress", "stop", "poll", "down", "query", ("close", 142)])

    def test_main_already_gone_with_stale_mainpid_still_cleans_survivors(self):
        # systemd may retain MAINPID after that PID has left the cgroup. It must
        # not obstruct automatic ExecStop or change the recorded failure result.
        self.os.environ.update(EXIT_STATUS="7", SERVICE_RESULT="exit-code")
        self.inventory.write_text("99\n42\n")
        self.poll_events = [[(142, select.POLLIN)]]
        self.assertEqual(self.main(), 0)
        self.os.pidfd_open.assert_called_once_with(42, 0)
        self.assertEqual(self.os.environ["EXIT_STATUS"], "7")
        self.assertEqual(self.events, [("open", 42), "ingress", "stop", "poll", "down", "query", ("close", 142)])

    def test_no_original_processes_still_stop_down_and_verify_engine(self):
        self.os.environ.clear()
        self.inventory.write_text("99\n")
        self.assertEqual(self.main(), 0)
        self.os.pidfd_open.assert_not_called()
        self.select.poll.assert_not_called()
        self.assertEqual(self.events, ["ingress", "stop", "down", "query"])
        self.assert_closed()

    def test_process_gone_before_pidfd_open_is_not_reprobed(self):
        self.os.pidfd_open.side_effect = [ProcessLookupError(PRIVATE_ERROR), 142]
        self.poll_events = [[(142, select.POLLIN)]]
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.os.pidfd_open.call_count, 2)
        self.poller.register.assert_called_once_with(142, select.POLLIN)
        self.assertLess(self.events.index("stop"), self.events.index("poll"))
        self.assert_closed(142)

    def test_all_originals_gone_during_capture_needs_no_pidfd_wait(self):
        def gone(pid, flags):
            self.inventory.write_text("99\n")
            raise ProcessLookupError(PRIVATE_ERROR)
        self.os.pidfd_open.side_effect = gone
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.os.pidfd_open.call_count, 2)
        self.select.poll.assert_not_called()
        self.assertEqual(self.events, ["ingress", "stop", "down", "query"])
        self.assert_closed()

    def test_pid_reused_outside_unit_does_not_make_original_pidfd_alive(self):
        # A numeric PID may now name an unrelated live process. Only the captured
        # pidfd's kernel readiness is authoritative; never probe/reopen that PID.
        self.os.kill = mock.Mock(return_value=None)  # Numeric probes report alive.
        self.os.pidfd_open.side_effect = lambda pid, flags: (
            pid + 900 if "stop" in self.events else self.open_pidfd(pid, flags)
        )
        # Only original descriptors 141/142 are readable, not reused identities.
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.os.pidfd_open.call_count, 2)
        self.os.kill.assert_not_called()
        self.assert_closed(141, 142)

    def test_reused_pid_inside_unit_is_rejected_even_when_old_pidfd_is_readable(self):
        self.after_exit = "99\n41\n"
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.poller.unregister.call_count, 2)
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_processlookup_does_not_exempt_pid_from_final_inventory(self):
        self.os.pidfd_open.side_effect = [ProcessLookupError(PRIVATE_ERROR), 142]
        self.poll_events = [[(142, select.POLLIN)]]
        self.after_exit = "99\n41\n"
        self.assertEqual(self.main(), 1)
        self.assertNotIn("down", self.events)
        self.assert_closed(142)

    def test_ingress_stop_error_prevents_all_stop_barrier_and_down(self):
        self.command_errors["ingress"] = subprocess.CalledProcessError(2, INGRESS, stderr=PRIVATE_ERROR)
        self.assertEqual(self.main(), 1)
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [INGRESS])
        self.poller.poll.assert_not_called()
        self.assert_closed(141, 142)

    def test_all_stop_error_prevents_barrier_and_down(self):
        self.command_errors["stop"] = subprocess.CalledProcessError(2, STOP, stderr=PRIVATE_ERROR)
        self.assertEqual(self.main(), 1)
        self.assertEqual([call.args[0] for call in self.run.call_args_list], [INGRESS, STOP])
        self.poller.poll.assert_not_called()
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_barrier_timeout_with_plugin_alive_prevents_down(self):
        self.time.monotonic.side_effect = [0.0, 0.0, 0.0, float(stop.BARRIER_TIMEOUT)]
        self.poll_events = [[(141, select.POLLIN)], []]
        self.assertEqual(self.main(), 1)
        self.poller.unregister.assert_called_once_with(141)
        self.assertLess(self.events.index("stop"), self.events.index("poll"))
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_interrupted_barrier_prevents_down(self):
        self.poller.poll.side_effect = InterruptedError(PRIVATE_ERROR)
        self.assertEqual(self.main(), 1)
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_keyboard_interrupt_is_quiet_and_closes_pidfds(self):
        self.poller.poll.side_effect = KeyboardInterrupt(PRIVATE_ERROR)
        self.assertEqual(self.main(), 1)
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_poll_errors_or_hangup_without_readability_prevent_down(self):
        for event in (select.POLLERR, select.POLLNVAL, select.POLLHUP,
                      select.POLLIN | select.POLLERR):
            with self.subTest(event=event):
                self.inventory.write_text("99\n41\n42\n")
                self.os.close.reset_mock()
                self.poll_events = [[(141, event)]]
                self.assertEqual(self.main(), 1)
                self.assertNotIn("down", self.events)
                self.assert_closed(141, 142)

    def test_unexpected_process_after_barrier_prevents_down(self):
        self.after_exit = "99\n77\n"
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.poller.unregister.call_count, 2)
        self.assertNotIn("down", self.events)
        self.assertIn("stop", self.events)
        self.assert_closed(141, 142)

    def test_missing_helper_in_final_inventory_fails_closed(self):
        self.after_exit = ""
        self.assertEqual(self.main(), 1)
        self.assertEqual(self.poller.unregister.call_count, 2)
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_down_failure_prevents_engine_query(self):
        self.command_errors["down"] = subprocess.CalledProcessError(2, DOWN, output=PRIVATE_ERROR)
        self.assertEqual(self.main(), 1)
        self.assertIn("down", self.events)
        self.assertNotIn("query", self.events)
        self.assert_closed(141, 142)

    def test_engine_query_failure_is_not_empty_success(self):
        self.command_errors["query"] = subprocess.CalledProcessError(1, QUERY, output=b"")
        self.assertEqual(self.main(), 1)
        self.assertIn("query", self.events)
        self.assert_closed(141, 142)

    def test_leftover_project_container_fails_quietly(self):
        self.query_output = b"synthetic-container-id\n"
        self.assertEqual(self.main(), 1)
        self.assertIn("query", self.events)
        self.assert_closed(141, 142)

    def test_partial_pidfd_capture_error_closes_prior_handles_without_stop(self):
        self.os.pidfd_open.side_effect = [141, PermissionError(PRIVATE_ERROR)]
        self.assertEqual(self.main(), 1)
        self.run.assert_not_called()
        self.assert_closed(141)

    def test_unavailable_linux_pidfd_support_fails_before_stop(self):
        del self.os.pidfd_open
        self.assertEqual(self.main(), 1)
        self.run.assert_not_called()
        self.assert_closed()

    def test_unexpected_command_exception_is_private_and_closes_handles(self):
        self.command_errors["stop"] = RuntimeError(PRIVATE_ERROR)
        self.assertEqual(self.main(), 1)
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_close_failure_does_not_skip_other_descriptors(self):
        self.os.close.side_effect = [OSError(PRIVATE_ERROR), None]
        self.assertEqual(self.main(), 1)
        self.assert_closed(141, 142)

    def test_duplicate_descriptors_are_closed_only_once(self):
        stop.close_pidfds([141, 141, 142, 142])
        self.assert_closed(141, 142)

    def test_root_required_before_reading_cgroup_or_running_commands(self):
        self.os.geteuid.return_value = 1000
        self.membership.unlink()
        self.assertEqual(self.main(), 1)
        self.os.pidfd_open.assert_not_called()
        self.run.assert_not_called()

    def test_exact_unified_service_membership_required(self):
        for membership in ("0::/\n", "0::/system.slice/other.service\n",
                           "0::/system.slice/business-tools.service/child\n",
                           "1:name=systemd:/system.slice/business-tools.service\n",
                           "0::/system.slice/business-tools.service\n1:cpu:/\n"):
            with self.subTest(membership=membership):
                self.membership.write_text(membership)
                self.assertEqual(self.main(), 1)
                self.os.pidfd_open.assert_not_called()
                self.run.assert_not_called()

    def test_missing_or_malformed_inventory_fails_before_commands(self):
        for contents in ("", "41\n42\n", "99\n0\n", "99\n-1\n", "99\ninvalid\n"):
            with self.subTest(contents=contents):
                self.inventory.write_text(contents)
                self.assertEqual(self.main(), 1)
                self.os.pidfd_open.assert_not_called()
                self.run.assert_not_called()

    def test_inventory_read_error_is_quiet_and_fail_closed(self):
        self.inventory.unlink()
        self.assertEqual(self.main(), 1)
        self.os.pidfd_open.assert_not_called()
        self.run.assert_not_called()

    def test_mainpid_is_ignored_even_when_invalid_or_stale(self):
        for value in ("", "0", "-1", " 41", "41\n", "４１", "invalid", "77", "99", "42"):
            with self.subTest(value=value):
                self.inventory.write_text("99\n41\n42\n")
                self.poll_events = [[(141, select.POLLIN), (142, select.POLLIN)]]
                self.os.pidfd_open.reset_mock()
                self.os.environ["MAINPID"] = value
                self.assertEqual(self.main(), 0)
                self.assertEqual(self.os.pidfd_open.call_args_list, [mock.call(41, 0), mock.call(42, 0)])

    def test_no_environment_read_or_signal_interface_is_required(self):
        del self.os.environ
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.poller.unregister.call_count, 2)
        source = (RUNTIME / "stop.py").read_text()
        self.assertNotIn("import signal", source)
        self.assertNotIn("pidfd_send_signal", source)
        self.assertNotIn("os.kill", source)

    def test_live_pid_reused_outside_unit_during_open_prevents_commands(self):
        def open_pidfd(pid, flags):
            self.inventory.write_text("99\n42\n")
            return self.open_pidfd(pid, flags)
        self.os.pidfd_open.side_effect = open_pidfd
        self.assertEqual(self.main(), 1)
        self.run.assert_not_called()
        self.assert_closed(141, 142)

    def test_new_member_during_capture_is_rejected_before_commands(self):
        def open_pidfd(pid, flags):
            self.inventory.write_text("99\n41\n42\n77\n")
            return self.open_pidfd(pid, flags)
        self.os.pidfd_open.side_effect = open_pidfd
        self.assertEqual(self.main(), 1)
        self.run.assert_not_called()
        self.assert_closed(141, 142)

    def test_main_exited_during_capture_remains_in_barrier(self):
        def open_pidfd(pid, flags):
            self.inventory.write_text("99\n42\n")
            return self.open_pidfd(pid, flags)
        self.os.pidfd_open.side_effect = open_pidfd
        self.capture_poller.poll.return_value = [(141, select.POLLIN)]
        self.assertEqual(self.main(), 0)
        self.assertLess(self.events.index("stop"), self.events.index("poll"))
        self.assertEqual(self.poller.unregister.call_count, 2)
        self.assert_closed(141, 142)

    def test_invalid_capture_readiness_fails_before_commands(self):
        self.capture_poller.poll.return_value = [(141, select.POLLERR)]
        self.assertEqual(self.main(), 1)
        self.run.assert_not_called()
        self.assert_closed(141, 142)

    def test_missing_mainpid_does_not_bypass_stop_errors(self):
        self.os.environ.clear()
        self.command_errors["stop"] = subprocess.CalledProcessError(2, STOP)
        self.assertEqual(self.main(), 1)
        self.poller.poll.assert_not_called()
        self.assertNotIn("down", self.events)
        self.assert_closed(141, 142)

    def test_new_member_during_stop_is_rejected_even_without_original_monitors(self):
        self.inventory.write_text("99\n")
        def command(args, **kwargs):
            if args == STOP:
                self.inventory.write_text("99\n77\n")
            return self.command(args, **kwargs)
        self.run.side_effect = command
        self.assertEqual(self.main(), 1)
        self.assertIn("stop", self.events)
        self.assertNotIn("down", self.events)
        self.assert_closed()

    def test_service_preserves_failure_and_foreground_ownership_contract(self):
        lines = (RUNTIME / "business-tools.service").read_text().splitlines()
        self.assertIn("ExecStop=/usr/bin/python3 /opt/business-tools/stop.py", lines)
        self.assertIn("Restart=always", lines)
        self.assertIn("TimeoutStopSec=4200", lines)
        start = next(line for line in lines if line.startswith("ExecStart="))
        self.assertTrue(start.endswith("up --remove-orphans --abort-on-container-exit"))
        self.assertFalse(any(line.startswith("SuccessExitStatus=") for line in lines))
        self.assertFalse(any(line.startswith(("KillSignal=", "KillMode=", "RestartPreventExitStatus=",
                                              "RestartForceExitStatus=", "ExecStopPost=")) for line in lines))


if __name__ == "__main__":
    unittest.main()