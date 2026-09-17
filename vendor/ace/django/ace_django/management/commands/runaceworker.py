"""Run supervised single-slot ACE activity workers."""

from __future__ import annotations

import signal
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from ace_django.exceptions import WorkerProcessExited
from ace_django.supervisor import WorkerSupervisor


class Command(BaseCommand):
    help = "Run spawned ACE activity workers coordinated through PostgreSQL."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--workers", type=int, default=4)
        parser.add_argument("--queues", default="medium")
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument("--lease-seconds", type=float, default=300.0)
        parser.add_argument("--renewal-seconds", type=float, default=60.0)
        parser.add_argument("--shutdown-timeout", type=float, default=30.0)
        parser.add_argument("--worker-id-prefix", default=None)

    def handle(self, *args, **options) -> None:
        queues = tuple(queue.strip() for queue in options["queues"].split(",") if queue.strip())
        supervisor = WorkerSupervisor(
            workers=options["workers"],
            queues=queues,
            poll_interval=options["poll_interval"],
            lease_duration=timedelta(seconds=options["lease_seconds"]),
            renewal_interval=timedelta(seconds=options["renewal_seconds"]),
            shutdown_timeout=options["shutdown_timeout"],
            worker_id_prefix=options["worker_id_prefix"],
        )

        def request_stop(signum, frame) -> None:
            self.stdout.write(f"ACE supervisor received signal {signum}; draining workers.")
            supervisor.request_stop()

        previous = {
            signal.SIGINT: signal.signal(signal.SIGINT, request_stop),
            signal.SIGTERM: signal.signal(signal.SIGTERM, request_stop),
        }
        self.stdout.write(
            self.style.SUCCESS(
                f"ACE supervisor starting {supervisor.workers} workers for {', '.join(queues)}."
            )
        )
        try:
            supervisor.run()
        except WorkerProcessExited as exc:
            raise CommandError(str(exc)) from exc
        finally:
            supervisor.shutdown()
            for signum, handler in previous.items():
                signal.signal(signum, handler)
