"""Spawn-safe entry points for PostgreSQL multi-process tests."""

from datetime import datetime, timedelta
from uuid import UUID

import django
from django.db import connections

# Spawn imports this module before invoking the target, so initialize Django
# before importing modules that define ORM models.
django.setup()

from ace_django.queue import DjangoActivityQueue  # noqa: E402
from ace_django.tests.helpers import (  # noqa: E402
    FROZEN_NOW,
    build_engine,
    build_fan_in_engine,
    build_group_engine,
)


def claim_activity(
    barrier,
    output,
    worker_id: str,
    ownership_token: str,
    now: str,
    lease_seconds: int = 30,
) -> None:
    connections.close_all()
    barrier.wait(timeout=10)
    queue = DjangoActivityQueue(token_factory=lambda: UUID(ownership_token))
    lease = queue.claim(
        worker_id,
        now=datetime.fromisoformat(now),
        lease_duration=timedelta(seconds=lease_seconds),
    )
    output.put(lease)
    connections.close_all()


def hold_activity_claim(output, release) -> None:
    """Keep one default-queue claim uncommitted to inspect its actual lock scope."""
    from django.db import transaction

    connections.close_all()
    try:
        with transaction.atomic():
            lease = DjangoActivityQueue().claim("held-claim", now=FROZEN_NOW)
            output.put(lease)
            assert release.wait(timeout=20), "Parent did not release the test claim"
    finally:
        connections.close_all()


def start_idempotent_workflow(
    barrier,
    output,
    run_id: str,
    event_id: str,
) -> None:
    connections.close_all()
    engine = build_engine(run_id, event_id)
    barrier.wait(timeout=10)
    started = engine.start(
        "adapter-test",
        {"subject": "shared-risk"},
        idempotency_key="shared-intake",
    )
    output.put(started.run_id)
    connections.close_all()


def complete_fan_in_activity(
    barrier,
    output,
    worker_id: str,
    ownership_token: str,
    event_id: str,
) -> None:
    connections.close_all()
    queue = DjangoActivityQueue(
        build_fan_in_engine(event_id),
        token_factory=lambda: UUID(ownership_token),
    )
    lease = queue.claim(worker_id, now=FROZEN_NOW, lease_duration=timedelta(seconds=5))
    barrier.wait(timeout=10)
    if lease is None:
        output.put(None)
    else:
        queue.complete(lease, {"worker_id": worker_id}, now=FROZEN_NOW)
        output.put(lease.activity_run_id)
    connections.close_all()


def complete_group_activity(
    barrier,
    output,
    worker_id: str,
    ownership_token: str,
    event_id: str,
) -> None:
    """Claim one group member, wait at barrier, then complete it."""
    connections.close_all()
    queue = DjangoActivityQueue(
        build_group_engine(event_id),
        token_factory=lambda: UUID(ownership_token),
    )
    lease = queue.claim(worker_id, now=FROZEN_NOW, lease_duration=timedelta(seconds=5))
    barrier.wait(timeout=10)
    if lease is None:
        output.put(None)
    else:
        snapshot = queue.complete(lease, {"worker_id": worker_id}, now=FROZEN_NOW)
        output.put(
            {
                "activity_run_id": lease.activity_run_id,
                "snapshot_status": snapshot.status if snapshot else None,
            }
        )
    connections.close_all()
