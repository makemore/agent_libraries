"""Single-slot ACE activity worker with renewable PostgreSQL leases."""

from __future__ import annotations

import logging
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

from ace import ActivityCancelled, ActivityContext, ActivityRegistry
from django.conf import settings
from django.db import connections
from django.utils import timezone
from django.utils.module_loading import import_string

from ace_django.exceptions import LeaseOwnershipLost, WorkerConfigurationError
from ace_django.models import AceWorkerHeartbeat, WorkerRole, WorkerStatus
from ace_django.queue import ActivityLease, DjangoActivityQueue
from ace_django.runtime import DjangoRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

    from ace import WorkflowEngine
    from ace.json_types import JsonObject

logger = logging.getLogger(__name__)


class StopSignal(Protocol):
    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@dataclass(frozen=True)
class WorkerRuntime:
    activities: ActivityRegistry
    workflow_engine: WorkflowEngine | None = None


def load_runtime() -> DjangoRuntime | WorkerRuntime:
    path = getattr(settings, "ACE_RUNTIME_FACTORY", None)
    if not isinstance(path, str) or not path.strip():
        raise WorkerConfigurationError(
            "ACE_RUNTIME_FACTORY must be a dotted path to a callable returning "
            "DjangoRuntime or WorkerRuntime."
        )
    factory = cast("Callable[[], object]", import_string(path))
    runtime = factory()
    if not isinstance(runtime, (DjangoRuntime, WorkerRuntime)):
        raise WorkerConfigurationError(
            f"ACE runtime factory {path!r} returned {type(runtime).__name__}, "
            "not DjangoRuntime or WorkerRuntime."
        )
    return runtime


class AceWorker:
    def __init__(
        self,
        runtime: DjangoRuntime | WorkerRuntime,
        *,
        worker_id: str | None = None,
        queues: tuple[str, ...] = ("medium",),
        poll_interval: float = 1.0,
        lease_duration: timedelta = timedelta(minutes=5),
        renewal_interval: timedelta = timedelta(minutes=1),
        stop_signal: StopSignal | None = None,
        now: Callable[[], datetime] = timezone.now,
    ) -> None:
        if not queues:
            raise WorkerConfigurationError("ACE worker must listen to at least one queue.")
        if poll_interval < 0:
            raise WorkerConfigurationError("ACE worker poll_interval cannot be negative.")
        if renewal_interval <= timedelta(0) or renewal_interval >= lease_duration:
            raise WorkerConfigurationError(
                "ACE worker renewal_interval must be positive and shorter than lease_duration."
            )
        hostname = socket.gethostname()
        self.worker_id = worker_id or f"ace-{hostname}-{os.getpid()}-{uuid4().hex[:8]}"
        self.queues = queues
        self.poll_interval = poll_interval
        self.lease_duration = lease_duration
        self.renewal_interval = renewal_interval
        self._runtime = runtime
        self._queue = DjangoActivityQueue(
            getattr(runtime, "workflow_engine", None),
            use_inbox=isinstance(runtime, DjangoRuntime),
        )
        self._stop = stop_signal or threading.Event()
        self._now = now
        self._hostname = hostname
        # Version routing: advertise and claim only registered activity versions.
        self._activity_capabilities = frozenset(runtime.activities.identities())
        self._workflow_capabilities = frozenset(
            getattr(runtime, "workflow_capabilities", frozenset())
        )
        self._deployment_id = getattr(runtime, "deployment_id", None)
        self._backend = getattr(runtime, "backend", "django")

    def run(self) -> None:
        logger.info("ACE worker %s starting for queues %s", self.worker_id, self.queues)
        self._record_heartbeat(WorkerStatus.ACTIVE)
        try:
            while not self._stop.is_set():
                worked = self.run_once()
                self._record_heartbeat(WorkerStatus.ACTIVE)
                if not worked:
                    self._stop.wait(self.poll_interval)
        finally:
            self._record_heartbeat(WorkerStatus.STOPPED)
            logger.info("ACE worker %s stopped", self.worker_id)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        if self._stop.is_set():
            return False
        lease = self._queue.claim(
            self.worker_id,
            queues=self.queues,
            lease_duration=self.lease_duration,
            now=self._now(),
            activity_capabilities=self._activity_capabilities,
        )
        if lease is None:
            return False
        self._execute(lease)
        return True

    def _execute(self, lease: ActivityLease) -> None:
        renewer = _LeaseRenewer(self, lease)
        renewer.start()
        context = ActivityContext(
            activity_run_id=lease.activity_run_id,
            operation_id=lease.ownership_token,
            workflow_run_id=lease.workflow_run_id,
            attempt=lease.attempt,
            heartbeat=lambda details=None: self._heartbeat(lease, details),
            cancellation_requested=lambda: self._queue.cancellation_requested(lease),
        )
        try:
            activity = self._runtime.activities.resolve(
                lease.activity_name,
                lease.activity_version,
            )
            result = activity(context, lease.input)
        except ActivityCancelled as exc:
            renewer.stop()
            self._cancel(lease, str(exc))
        except LeaseOwnershipLost:
            renewer.stop()
            logger.warning("ACE worker %s lost lease for %s", self.worker_id, lease.activity_run_id)
        except Exception as exc:
            renewer.stop()
            self._fail(lease, exc)
        else:
            renewer.stop()
            if renewer.ownership_lost:
                logger.warning(
                    "ACE worker %s discarded result after losing lease for %s",
                    self.worker_id,
                    lease.activity_run_id,
                )
                return
            try:
                self._queue.complete(lease, result, now=self._now())
            except LeaseOwnershipLost:
                logger.warning(
                    "ACE worker %s could not complete stale lease for %s",
                    self.worker_id,
                    lease.activity_run_id,
                )
        finally:
            renewer.stop()

    def _heartbeat(self, lease: ActivityLease, details: JsonObject | None = None) -> None:
        if not self._renew(lease, details):
            raise LeaseOwnershipLost(
                f"Activity {lease.activity_run_id!r} attempt {lease.attempt} lost its lease."
            )

    def _renew(self, lease: ActivityLease, details: JsonObject | None = None) -> bool:
        renewed = self._queue.renew(
            lease,
            details=details,
            lease_duration=self.lease_duration,
            now=self._now(),
        )
        status = WorkerStatus.DRAINING if self._stop.is_set() else WorkerStatus.ACTIVE
        self._record_heartbeat(status)
        return renewed

    def _cancel(self, lease: ActivityLease, message: str) -> None:
        try:
            self._queue.cancel(lease, message=message, now=self._now())
        except LeaseOwnershipLost:
            logger.warning(
                "ACE worker %s could not cancel stale lease %s",
                self.worker_id,
                lease.activity_run_id,
            )

    def _fail(self, lease: ActivityLease, exc: Exception) -> None:
        logger.exception("ACE activity %s failed", lease.activity_run_id, exc_info=exc)
        try:
            self._queue.fail(
                lease,
                error_type=type(exc).__name__,
                message=str(exc),
                now=self._now(),
            )
        except LeaseOwnershipLost:
            logger.warning(
                "ACE worker %s could not fail stale lease %s", self.worker_id, lease.activity_run_id
            )

    def _record_heartbeat(self, status: WorkerStatus) -> None:
        AceWorkerHeartbeat.objects.update_or_create(
            worker_id=self.worker_id,
            defaults={
                "process_id": os.getpid(),
                "hostname": self._hostname,
                "queues": list(self.queues),
                "status": status,
                "last_seen_at": self._now(),
                "role": WorkerRole.ACTIVITY,
                "deployment_id": self._deployment_id,
                "backend": self._backend,
                "workflow_capabilities": sorted(list(pair) for pair in self._workflow_capabilities),
                "activity_capabilities": sorted(list(pair) for pair in self._activity_capabilities),
            },
        )


class _LeaseRenewer:
    def __init__(self, worker: AceWorker, lease: ActivityLease) -> None:
        self._worker = worker
        self._lease = lease
        self._done = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"ace-lease-{lease.activity_run_id}",
            daemon=True,
        )

    @property
    def ownership_lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._worker.renewal_interval.total_seconds() + 1)

    def _run(self) -> None:
        connections.close_all()
        try:
            while not self._done.wait(self._worker.renewal_interval.total_seconds()):
                if not self._worker._renew(self._lease):
                    self._lost.set()
                    return
        except Exception:
            self._lost.set()
            logger.exception("ACE lease renewal failed for %s", self._lease.activity_run_id)
        finally:
            connections.close_all()
