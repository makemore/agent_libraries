"""Role-tagged heartbeats for ACE service processes.

Activity workers record their own heartbeats in ``AceWorker``; the
dispatcher, timer, deadline, and activity-timeout services are driven by
``runaceservices`` which records heartbeats through this helper so
``collect_ace_readiness`` can verify each role is alive.
"""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

from django.utils import timezone

from ace_django.models import AceWorkerHeartbeat, WorkerRole, WorkerStatus

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


def record_service_heartbeat(
    worker_id: str,
    role: WorkerRole,
    *,
    status: WorkerStatus = WorkerStatus.ACTIVE,
    deployment_id: str | None = None,
    backend: str = "django",
    workflow_capabilities: Iterable[tuple[str, str]] = (),
    activity_capabilities: Iterable[tuple[str, str]] = (),
    now: datetime | None = None,
) -> AceWorkerHeartbeat:
    """Record (or refresh) a heartbeat row for a service process."""
    heartbeat, _created = AceWorkerHeartbeat.objects.update_or_create(
        worker_id=worker_id,
        defaults={
            "process_id": os.getpid(),
            "hostname": socket.gethostname(),
            "queues": [],
            "status": status,
            "last_seen_at": now or timezone.now(),
            "role": role,
            "deployment_id": deployment_id,
            "backend": backend,
            "workflow_capabilities": sorted(list(pair) for pair in workflow_capabilities),
            "activity_capabilities": sorted(list(pair) for pair in activity_capabilities),
        },
    )
    return heartbeat
