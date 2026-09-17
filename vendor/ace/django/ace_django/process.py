"""Spawn-safe ACE worker process entry point."""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING

import django
from django.db import connections

if TYPE_CHECKING:
    from datetime import timedelta

# Spawn imports this module in a fresh interpreter before invoking the target.
django.setup()

from ace_django.worker import AceWorker, load_runtime  # noqa: E402


def run_worker_process(
    stop_signal,
    worker_id: str,
    queues: tuple[str, ...],
    poll_interval: float,
    lease_duration: timedelta,
    renewal_interval: timedelta,
) -> None:
    # Terminal Ctrl+C delivers SIGINT to the whole foreground process group,
    # so workers would die with raw KeyboardInterrupt tracebacks mid-drain.
    # Shutdown is coordinated by the supervisor: it sets stop_signal (clean
    # drain) and falls back to terminate() (SIGTERM) after shutdown_timeout.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    connections.close_all()
    worker = AceWorker(
        load_runtime(),
        worker_id=worker_id,
        queues=queues,
        poll_interval=poll_interval,
        lease_duration=lease_duration,
        renewal_interval=renewal_interval,
        stop_signal=stop_signal,
    )
    try:
        worker.run()
    finally:
        connections.close_all()
