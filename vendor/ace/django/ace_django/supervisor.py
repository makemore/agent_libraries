"""Spawned multi-process supervisor for single-slot ACE workers."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from datetime import timedelta
from multiprocessing import get_context
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import connections

from ace_django.exceptions import WorkerConfigurationError, WorkerProcessExited
from ace_django.process import run_worker_process

if TYPE_CHECKING:
    from multiprocessing.context import BaseContext
    from multiprocessing.process import BaseProcess

logger = logging.getLogger(__name__)


class WorkerSupervisor:
    def __init__(
        self,
        *,
        workers: int = 4,
        queues: tuple[str, ...] = ("medium",),
        poll_interval: float = 1.0,
        lease_duration: timedelta = timedelta(minutes=5),
        renewal_interval: timedelta = timedelta(minutes=1),
        shutdown_timeout: float = 30.0,
        worker_id_prefix: str | None = None,
        context: BaseContext | None = None,
        process_target=run_worker_process,
    ) -> None:
        if workers < 1:
            raise WorkerConfigurationError("ACE supervisor workers must be at least 1.")
        if shutdown_timeout <= 0:
            raise WorkerConfigurationError("ACE supervisor shutdown_timeout must be positive.")
        self.workers = workers
        self.queues = queues
        self.poll_interval = poll_interval
        self.lease_duration = lease_duration
        self.renewal_interval = renewal_interval
        self.shutdown_timeout = shutdown_timeout
        self.worker_id_prefix = worker_id_prefix or (
            f"ace-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self._context = context or get_context("spawn")
        self._target = process_target
        self._stop = self._context.Event()
        self._stop_requested = threading.Event()
        self._processes: list[BaseProcess] = []

    @property
    def processes(self) -> tuple[BaseProcess, ...]:
        return tuple(self._processes)

    def start(self) -> None:
        if self._processes:
            raise WorkerConfigurationError("ACE supervisor cannot be started twice.")
        connections.close_all()
        try:
            for index in range(self.workers):
                worker_id = f"{self.worker_id_prefix}-{index + 1}"
                process = self._context.Process(
                    name=worker_id,
                    target=self._target,
                    args=(
                        self._stop,
                        worker_id,
                        self.queues,
                        self.poll_interval,
                        self.lease_duration,
                        self.renewal_interval,
                    ),
                )
                process.start()
                self._processes.append(process)
        except BaseException:
            self.shutdown()
            raise
        logger.info("ACE supervisor started %d workers", self.workers)

    def run(self) -> None:
        self.start()
        self.wait()

    def wait(self) -> None:
        if not self._processes:
            raise WorkerConfigurationError("ACE supervisor must be started before wait().")
        try:
            while True:
                exited = [process for process in self._processes if process.exitcode is not None]
                if self._stop_requested.is_set() or self._stop.is_set():
                    self.shutdown()
                    self._raise_for_failed_process(self._processes)
                    return
                elif exited:
                    process = exited[0]
                    raise WorkerProcessExited(
                        f"ACE worker process {process.name!r} exited unexpectedly "
                        f"with code {process.exitcode}."
                    )
                self._stop_requested.wait(0.1)
        except BaseException:
            self.shutdown()
            raise

    def request_stop(self) -> None:
        # Signal-handler safe: only sets a local threading.Event. Setting the
        # shared multiprocessing Event from a signal handler can self-deadlock —
        # mp.Event.set() -> Condition.notify_all() blocks on _woken_count until
        # the sleeper wakes, but the sleeper is the interrupted frame the handler
        # is running on top of. The mp event is set in shutdown() from the wait
        # loop, in normal (non-handler) context.
        logger.info("ACE supervisor draining %d workers", len(self._processes))
        self._stop_requested.set()

    def shutdown(self) -> None:
        self._stop_requested.set()
        self._stop.set()
        deadline = time.monotonic() + self.shutdown_timeout
        for process in self._processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
        for process in self._processes:
            if process.is_alive():
                logger.error("ACE worker %s exceeded shutdown timeout; terminating", process.name)
                process.terminate()
        for process in self._processes:
            process.join(timeout=1.0)
        connections.close_all()

    @staticmethod
    def _raise_for_failed_process(processes: list[BaseProcess]) -> None:
        failed = [process for process in processes if process.exitcode != 0]
        if failed:
            details = ", ".join(f"{process.name}={process.exitcode}" for process in failed)
            raise WorkerProcessExited(f"ACE worker processes failed during shutdown: {details}.")
