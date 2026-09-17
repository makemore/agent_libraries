"""Run ACE background services (dispatcher, timer, deadline, activity timeouts).

Each enabled role polls in its own thread and records a role-tagged
heartbeat every iteration so ``collect_ace_readiness`` can verify the
service is alive. SIGINT/SIGTERM trigger a graceful drain.
"""

from __future__ import annotations

import logging
import signal
import threading
import uuid

from django.core.management.base import BaseCommand, CommandError

from ace_django.dispatcher import DjangoWorkflowDispatcher
from ace_django.deadlines import DjangoDeadlineService
from ace_django.activity_timeouts import DjangoActivityTimeoutService
from ace_django.heartbeats import record_service_heartbeat
from ace_django.models import WorkerRole, WorkerStatus
from ace_django.runtime import load_runtime
from ace_django.store import DjangoExecutionStore
from ace_django.timers import DjangoTimerService

logger = logging.getLogger(__name__)

VALID_ROLES = ("dispatcher", "timer", "deadline", "timeouts")

ROLE_TO_HEARTBEAT = {
    "dispatcher": WorkerRole.DISPATCHER,
    "timer": WorkerRole.TIMER,
    "deadline": WorkerRole.DEADLINE,
    "timeouts": WorkerRole.DEADLINE,
}


class Command(BaseCommand):
    help = "Run ACE dispatcher, timer, deadline, and activity-timeout services."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--roles",
            default="dispatcher,timer,deadline,timeouts",
            help=f"Comma-separated roles to run ({', '.join(VALID_ROLES)}).",
        )
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument("--worker-id-prefix", default=None)
        parser.add_argument(
            "--max-iterations",
            type=int,
            default=None,
            help="Stop each role after this many idle poll iterations (testing).",
        )

    def handle(self, *args, **options) -> None:
        roles = tuple(r.strip() for r in options["roles"].split(",") if r.strip())
        invalid = [r for r in roles if r not in VALID_ROLES]
        if invalid:
            raise CommandError(f"Unknown roles: {', '.join(invalid)}")
        if not roles:
            raise CommandError("At least one role must be selected.")

        runtime = None
        if "dispatcher" in roles:
            runtime = load_runtime()
            if runtime.workflows is None:
                raise CommandError(
                    "The dispatcher role requires ACE_RUNTIME_FACTORY to provide a "
                    "DjangoRuntime with a workflow registry."
                )

        prefix = options["worker_id_prefix"] or f"ace-services-{uuid.uuid4().hex[:8]}"
        poll_interval = options["poll_interval"]
        max_iterations = options["max_iterations"]
        stop = threading.Event()

        def request_stop(signum, frame) -> None:
            self.stdout.write(f"ACE services received signal {signum}; draining.")
            stop.set()

        previous = {
            signal.SIGINT: signal.signal(signal.SIGINT, request_stop),
            signal.SIGTERM: signal.signal(signal.SIGTERM, request_stop),
        }

        step_fns = {
            "timer": lambda service=DjangoTimerService(): service.deliver_once(),
            "deadline": lambda service=DjangoDeadlineService(): service.enforce_once(),
            "timeouts": lambda service=DjangoActivityTimeoutService(): service.enforce_once(),
        }
        if runtime is not None:
            step_fns["dispatcher"] = self._dispatcher_step(runtime)

        threads = []
        try:
            for role in roles:
                thread = threading.Thread(
                    target=self._run_role,
                    name=f"ace-{role}",
                    args=(
                        role,
                        step_fns[role],
                        f"{prefix}-{role}",
                        stop,
                        poll_interval,
                        max_iterations,
                    ),
                    daemon=True,
                )
                thread.start()
                threads.append(thread)
            self.stdout.write(f"ACE services started: {', '.join(roles)}")
            for thread in threads:
                while thread.is_alive():
                    thread.join(timeout=0.2)
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=10)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            self.stdout.write("ACE services stopped.")

    @staticmethod
    def _dispatcher_step(runtime):
        dispatcher = DjangoWorkflowDispatcher(runtime.workflows, DjangoExecutionStore())
        return dispatcher.dispatch_once

    @staticmethod
    def _run_role(role, step, worker_id, stop, poll_interval, max_iterations) -> None:
        from django.db import connections

        heartbeat_role = ROLE_TO_HEARTBEAT[role]
        idle_iterations = 0
        try:
            while not stop.is_set():
                _record_heartbeat_safely(worker_id, heartbeat_role, role)
                try:
                    worked = step() is not None
                except Exception:
                    logger.exception("ACE %s service iteration failed", role)
                    worked = False
                if worked:
                    idle_iterations = 0
                    continue
                idle_iterations += 1
                if max_iterations is not None and idle_iterations >= max_iterations:
                    return
                stop.wait(poll_interval)
        finally:
            _record_heartbeat_safely(worker_id, heartbeat_role, role, status=WorkerStatus.STOPPED)
            connections.close_all()


def _record_heartbeat_safely(
    worker_id, heartbeat_role, role, *, status=WorkerStatus.ACTIVE
) -> None:
    """Record a heartbeat; a transient DB error must not kill the service loop."""
    try:
        record_service_heartbeat(worker_id, heartbeat_role, status=status)
    except Exception:
        logger.exception("ACE %s service could not record heartbeat", role)
