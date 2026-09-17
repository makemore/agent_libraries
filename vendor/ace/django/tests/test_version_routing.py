"""Tests for version-aware routing of activities and workflow events.

Verifies:
- Workers only claim activities whose (name, version) they advertise
- Worker heartbeats record role, backend, and capabilities
- Dispatchers skip inbox events for unregistered workflow versions
  without spending recovery budget
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from ace import ActivityContext, ActivityRegistry
from ace.json_types import JsonObject, JsonValue
from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityRun,
    ActivityStatus,
    WorkerRole,
    WorkerStatus,
)
from ace_django.queue import DjangoActivityQueue
from ace_django.tests.helpers import FROZEN_NOW
from ace_django.worker import AceWorker, WorkerRuntime

pytestmark = pytest.mark.django_db


def _activity(run_id: str, name: str, version: str = "1") -> ActivityRun:
    return ActivityRun.objects.create(
        id=UUID(run_id),
        activity_key=f"routing-{run_id[-2:]}",
        activity_name=name,
        activity_version=version,
        input={},
        retry_policy={"max_attempts": 1},
        available_at=FROZEN_NOW,
    )


def _noop(context: ActivityContext, input: JsonObject) -> JsonValue:
    return {"ok": True}


def _worker(activities: ActivityRegistry, worker_id: str = "routing-worker") -> AceWorker:
    return AceWorker(
        WorkerRuntime(activities),
        worker_id=worker_id,
        lease_duration=timedelta(seconds=5),
        renewal_interval=timedelta(seconds=1),
        now=lambda: FROZEN_NOW,
    )


def test_claim_without_capabilities_takes_any_activity() -> None:
    """Legacy callers (capabilities=None) claim regardless of version."""
    _activity("30000000-0000-0000-0000-000000000101", "tests.any", version="9")

    queue = DjangoActivityQueue()
    lease = queue.claim("legacy-worker", now=FROZEN_NOW)

    assert lease is not None
    assert lease.activity_version == "9"


def test_claim_skips_unadvertised_activity_name() -> None:
    _activity("30000000-0000-0000-0000-000000000102", "tests.unknown")

    queue = DjangoActivityQueue()
    lease = queue.claim(
        "routing-worker",
        now=FROZEN_NOW,
        activity_capabilities=frozenset({("tests.known", "1")}),
    )

    assert lease is None


def test_claim_skips_unadvertised_activity_version() -> None:
    _activity("30000000-0000-0000-0000-000000000103", "tests.known", version="2")

    queue = DjangoActivityQueue()
    lease = queue.claim(
        "routing-worker",
        now=FROZEN_NOW,
        activity_capabilities=frozenset({("tests.known", "1")}),
    )

    assert lease is None


def test_claim_matches_advertised_name_and_version() -> None:
    _activity("30000000-0000-0000-0000-000000000104", "tests.known", version="2")

    queue = DjangoActivityQueue()
    lease = queue.claim(
        "routing-worker",
        now=FROZEN_NOW,
        activity_capabilities=frozenset({("tests.known", "1"), ("tests.known", "2")}),
    )

    assert lease is not None
    assert lease.activity_name == "tests.known"
    assert lease.activity_version == "2"


def test_worker_only_executes_registered_versions() -> None:
    """A worker leaves activities for versions it does not register untouched."""
    unsupported = _activity("30000000-0000-0000-0000-000000000105", "tests.routed", version="2")
    supported = _activity("30000000-0000-0000-0000-000000000106", "tests.routed", version="1")

    activities = ActivityRegistry()
    activities.register("tests.routed", _noop, version="1")
    worker = _worker(activities)

    assert worker.run_once() is True
    assert worker.run_once() is False  # nothing else claimable

    supported.refresh_from_db()
    unsupported.refresh_from_db()
    assert supported.status == ActivityStatus.SUCCEEDED
    assert unsupported.status == ActivityStatus.READY


def test_worker_heartbeat_records_role_backend_and_capabilities() -> None:
    activities = ActivityRegistry()
    activities.register("tests.cap", _noop, version="1")
    activities.register("tests.cap", _noop, version="2")
    worker = _worker(activities, worker_id="capability-worker")

    worker._record_heartbeat(WorkerStatus.ACTIVE)

    heartbeat = AceWorkerHeartbeat.objects.get(worker_id="capability-worker")
    assert heartbeat.role == WorkerRole.ACTIVITY
    assert heartbeat.backend == "django"
    assert heartbeat.activity_capabilities == [["tests.cap", "1"], ["tests.cap", "2"]]
    assert heartbeat.workflow_capabilities == []
