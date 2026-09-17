from io import StringIO
from multiprocessing import get_context
from unittest.mock import patch

import pytest
from ace_django.exceptions import WorkerConfigurationError, WorkerProcessExited
from ace_django.supervisor import WorkerSupervisor
from ace_django.tests.worker_helpers import exit_immediately, hang_until_killed
from django.core.management import call_command
from django.core.management.base import CommandError


def test_supervisor_rejects_invalid_worker_count() -> None:
    with pytest.raises(WorkerConfigurationError, match="at least 1"):
        WorkerSupervisor(workers=0)


def test_supervisor_surfaces_unexpected_child_failure() -> None:
    supervisor = WorkerSupervisor(
        workers=1,
        worker_id_prefix="failure-test",
        shutdown_timeout=1,
        context=get_context("spawn"),
        process_target=exit_immediately,
    )
    supervisor.start()

    with pytest.raises(WorkerProcessExited, match="code 7"):
        supervisor.wait()

    assert supervisor.processes[0].exitcode == 7


def test_supervisor_enforces_shutdown_timeout() -> None:
    supervisor = WorkerSupervisor(
        workers=1,
        worker_id_prefix="timeout-test",
        shutdown_timeout=0.1,
        context=get_context("spawn"),
        process_target=hang_until_killed,
    )
    supervisor.start()
    supervisor.request_stop()

    with pytest.raises(WorkerProcessExited, match="failed during shutdown"):
        supervisor.wait()

    assert supervisor.processes[0].is_alive() is False


@patch("ace_django.management.commands.runaceworker.WorkerSupervisor")
def test_management_command_builds_and_runs_supervisor(supervisor_class) -> None:
    supervisor = supervisor_class.return_value
    supervisor.workers = 2

    call_command(
        "runaceworker",
        "--workers=2",
        "--queues=high,medium",
        "--poll-interval=0.25",
        stdout=StringIO(),
    )

    supervisor_class.assert_called_once()
    options = supervisor_class.call_args.kwargs
    assert options["workers"] == 2
    assert options["queues"] == ("high", "medium")
    assert options["poll_interval"] == 0.25
    supervisor.run.assert_called_once_with()
    supervisor.shutdown.assert_called_once_with()


@patch("ace_django.management.commands.runaceworker.WorkerSupervisor")
def test_management_command_translates_child_failure(supervisor_class) -> None:
    supervisor = supervisor_class.return_value
    supervisor.workers = 1
    supervisor.run.side_effect = WorkerProcessExited("worker failed")

    with pytest.raises(CommandError, match="worker failed"):
        call_command("runaceworker", stdout=StringIO())

    supervisor.shutdown.assert_called_once_with()
