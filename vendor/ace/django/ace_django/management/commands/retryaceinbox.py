"""Retry or reset dead-lettered inbox events."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ace_django.models import InboxStatus, WorkflowInboxEvent, WorkflowRun
from ace import WorkflowStatus


class Command(BaseCommand):
    help = "Retry or reset dead-lettered inbox events for recovery."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "run_id",
            nargs="?",
            help="Specific workflow run ID to retry (optional)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_runs",
            help="Process all dead-lettered events across all runs",
        )
        parser.add_argument(
            "--max-count",
            type=int,
            default=100,
            help="Maximum number of events to process (default: 100)",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        all_runs = options["all_runs"]
        max_count = options["max_count"]
        run_id = options.get("run_id")

        if not run_id and not all_runs:
            raise CommandError("Specify a run_id or use --all to process all runs")

        if run_id and all_runs:
            raise CommandError("Cannot specify both run_id and --all")

        # Find dead-lettered events
        queryset = WorkflowInboxEvent.objects.filter(
            status=InboxStatus.DEAD_LETTER,
        )

        if run_id:
            queryset = queryset.filter(workflow_run_id=run_id)

        # Exclude terminal/blocked runs - they need resume first
        queryset = queryset.exclude(
            workflow_run__status__in=[
                WorkflowStatus.COMPLETED.value,
                WorkflowStatus.FAILED.value,
                WorkflowStatus.CANCELLED.value,
                WorkflowStatus.BLOCKED.value,
            ]
        ).order_by("created_at")[:max_count]

        events = list(queryset)

        if not events:
            self.stdout.write("No dead-lettered events found matching criteria")
            return

        self.stdout.write(f"Found {len(events)} dead-lettered event(s)")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] Would reset:"))
            for event in events:
                self.stdout.write(
                    f"  - Run {event.workflow_run_id} / seq {event.inbox_sequence}: "
                    f"{event.source_type}:{event.source_key} ({event.event_type})"
                )
            return

        # Reset events to RETRYING with immediate availability
        reset_count = 0
        now = timezone.now()

        for event in events:
            with transaction.atomic():
                # Lock the event
                locked = WorkflowInboxEvent.objects.select_for_update().get(pk=event.pk)
                if locked.status != InboxStatus.DEAD_LETTER:
                    self.stdout.write(f"  - Skipping {event.pk}: status changed")
                    continue

                locked.status = InboxStatus.RETRYING
                locked.available_at = now
                locked.attempts = 0  # Reset retry counter
                locked.save(update_fields=["status", "available_at", "attempts", "updated_at"])
                reset_count += 1
                self.stdout.write(f"  - Reset {event.workflow_run_id} / seq {event.inbox_sequence}")

        self.stdout.write(self.style.SUCCESS(f"\nReset {reset_count} event(s) for retry"))
