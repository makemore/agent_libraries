"""Report ACE queue and worker health without mutating execution state."""

from __future__ import annotations

import json
from dataclasses import asdict

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ace_django.health import (
    AceHealthReport,
    QueueReadiness,
    collect_ace_failure_metrics,
    collect_ace_health,
    collect_ace_readiness,
    collect_queue_readiness,
)


def _setting(name: str, default):
    """Read an ACE_* setting, falling back to the legacy SUBMISSION_ACE_* name."""
    legacy = f"SUBMISSION_{name}"
    return getattr(settings, name, getattr(settings, legacy, default))


def _serialize(report: AceHealthReport) -> dict[str, object]:
    return {
        "checked_at": report.checked_at.isoformat(),
        "healthy": report.healthy,
        "workers_expected": report.workers_expected,
        "queues": [
            {
                "queue": queue.queue,
                "depth": queue.depth,
                "ready_depth": queue.ready_depth,
                "oldest_ready_at": (
                    queue.oldest_ready_at.isoformat() if queue.oldest_ready_at else None
                ),
                "oldest_ready_age_seconds": queue.oldest_ready_age_seconds,
                "has_fresh_worker": queue.has_fresh_worker,
                "expired_active_leases": queue.expired_active_leases,
                "routing_ready": queue.routing_ready,
                "routing_reasons": list(queue.routing_reasons),
            }
            for queue in report.queues
        ],
        "expired_leases": report.expired_leases,
        "failed_workflows": report.failed_workflows,
        "failed_activities": report.failed_activities,
        "failed_groups": report.failed_groups,
        "stale_heartbeats": report.stale_heartbeats,
        "unhealthy_reasons": list(report.unhealthy_reasons),
    }


def _serialize_readiness_compact(readiness: QueueReadiness) -> dict[str, object]:
    """Compact single-queue JSON with top-level routing fields for cloud logs."""
    return {
        "queue": readiness.queue,
        "routing_ready": readiness.routing_ready,
        "routing_reasons": list(readiness.routing_reasons),
        "ready_depth": readiness.ready_depth,
        "oldest_ready_age_seconds": readiness.oldest_ready_age_seconds,
        "has_fresh_worker": readiness.has_fresh_worker,
        "expired_active_leases": readiness.expired_active_leases,
    }


class Command(BaseCommand):
    help = "Report ACE queue depth, leases, failures, and worker heartbeat health."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--fail-on-unhealthy", action="store_true")
        parser.add_argument("--fail-on-routing-unready", action="store_true")
        parser.add_argument("--fail-on-unready", action="store_true")
        parser.add_argument("--queue", type=str, default=None)
        parser.add_argument("--compact", action="store_true")
        parser.add_argument(
            "--readiness",
            action="store_true",
            help="Report system readiness (services, routing); historical failures ignored.",
        )
        parser.add_argument(
            "--failure-metrics",
            action="store_true",
            help="Report historical failure counts for alerting.",
        )

    def handle(self, *args, **options) -> None:
        queue_name = options.get("queue")
        compact = options.get("compact", False)

        if options["readiness"]:
            readiness = collect_ace_readiness(
                heartbeat_stale_seconds=_setting("ACE_HEARTBEAT_STALE_SECONDS", 60),
            )
            payload = asdict(readiness)
            payload["checked_at"] = readiness.checked_at.isoformat()
            payload["reasons"] = list(readiness.reasons)
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            if options["fail_on_unready"] and not readiness.ready:
                raise CommandError(
                    f"ACE is not ready to accept work: {', '.join(readiness.reasons)}"
                )
            return

        if options["failure_metrics"]:
            metrics = collect_ace_failure_metrics()
            payload = asdict(metrics)
            payload["checked_at"] = metrics.checked_at.isoformat()
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        if queue_name and compact:
            readiness = collect_queue_readiness(
                queue_name,
                heartbeat_stale_seconds=_setting("ACE_HEARTBEAT_STALE_SECONDS", 60),
                oldest_ready_seconds=_setting("ACE_OLDEST_READY_SECONDS", 300),
                workers_expected=_setting("ACE_WORKERS_EXPECTED", False),
            )
            self.stdout.write(json.dumps(_serialize_readiness_compact(readiness), sort_keys=True))
            if options["fail_on_routing_unready"] and not readiness.routing_ready:
                raise CommandError(
                    f"ACE queue {queue_name!r} is not routing-ready: "
                    f"{', '.join(readiness.routing_reasons)}"
                )
            return

        report = collect_ace_health(
            expected_queues=tuple(_setting("ACE_EXPECTED_QUEUES", ())),
            heartbeat_stale_seconds=_setting("ACE_HEARTBEAT_STALE_SECONDS", 60),
            oldest_ready_seconds=_setting("ACE_OLDEST_READY_SECONDS", 300),
            workers_expected=_setting("ACE_WORKERS_EXPECTED", False),
        )
        self.stdout.write(json.dumps(_serialize(report), indent=2, sort_keys=True))

        if options["fail_on_unhealthy"] and not report.healthy:
            raise CommandError("ACE health check found unhealthy execution state.")

        if options["fail_on_routing_unready"] and queue_name:
            queue_health = next((q for q in report.queues if q.queue == queue_name), None)
            if queue_health is None or not queue_health.routing_ready:
                raise CommandError(f"ACE queue {queue_name!r} is not routing-ready.")
