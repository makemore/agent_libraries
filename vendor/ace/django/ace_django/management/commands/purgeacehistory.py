"""Purge old terminal workflow history for data retention.

SAFETY CONSTRAINTS:
- Dry-run by default; requires --confirm to actually delete.
- Only deletes terminal workflow runs (COMPLETED, FAILED, CANCELLED).
- Never deletes BLOCKED runs or runs with unprocessed inbox/DLQ events.
- Requires minimum age (--min-age-days).
- Deletes whole runs, not individual events (preserves replay capability).
- Processes in batches to avoid long transactions.
"""

from __future__ import annotations

from datetime import timedelta

from ace import WorkflowStatus
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ace_django.models import (
    InboxStatus,
    TimerStatus,
    WorkflowEvent,
    WorkflowInboxEvent,
    WorkflowRun,
    WorkflowTimer,
)


class Command(BaseCommand):
    help = "Purge old terminal workflow history for data retention."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--min-age-days",
            type=int,
            required=True,
            help="Minimum age in days for workflows to be purged.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of workflows to delete per batch (default: 100).",
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=None,
            help="Maximum number of batches to process (default: unlimited).",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Actually delete. Without this flag, only shows what would be deleted.",
        )
        parser.add_argument(
            "--namespace",
            type=str,
            default=None,
            help="Only purge workflows in this namespace.",
        )

    def handle(self, *args, **options) -> None:
        min_age_days = options["min_age_days"]
        batch_size = options["batch_size"]
        max_batches = options["max_batches"]
        confirm = options["confirm"]
        namespace = options["namespace"]

        if min_age_days < 1:
            raise CommandError("--min-age-days must be at least 1.")

        now = timezone.now()
        cutoff = now - timedelta(days=min_age_days)

        # Find purgeable workflows
        terminal_statuses = [s.value for s in WorkflowStatus if s.is_terminal]

        base_query = WorkflowRun.objects.filter(
            status__in=terminal_statuses,
            completed_at__lt=cutoff,
        )

        if namespace:
            base_query = base_query.filter(namespace=namespace)

        # Exclude BLOCKED (not terminal but just in case)
        base_query = base_query.exclude(status=WorkflowStatus.BLOCKED)

        # Get IDs of runs with pending inbox events
        runs_with_pending_inbox = set(
            WorkflowInboxEvent.objects.filter(
                status__in=(
                    InboxStatus.PENDING,
                    InboxStatus.RETRYING,
                    InboxStatus.DEAD_LETTER,
                )
            )
            .values_list("workflow_run_id", flat=True)
            .distinct()
        )

        # Get IDs of runs with pending timers
        runs_with_pending_timers = set(
            WorkflowTimer.objects.filter(
                status__in=(
                    TimerStatus.SCHEDULED,
                    TimerStatus.RETRYING,
                    TimerStatus.DEAD_LETTER,
                )
            )
            .values_list("workflow_run_id", flat=True)
            .distinct()
        )

        # Exclude those runs
        excluded_run_ids = runs_with_pending_inbox | runs_with_pending_timers
        if excluded_run_ids:
            base_query = base_query.exclude(pk__in=excluded_run_ids)

        total_purgeable = base_query.count()

        if total_purgeable == 0:
            self.stdout.write("No workflows eligible for purging.")
            return

        prefix = "[DRY RUN] " if not confirm else ""
        self.stdout.write(f"{prefix}Found {total_purgeable} workflows eligible for purging.")

        batches_processed = 0
        total_deleted = 0

        if confirm:
            # Actually delete in batches
            while True:
                if max_batches is not None and batches_processed >= max_batches:
                    self.stdout.write(f"{prefix}Reached max batches limit ({max_batches}).")
                    break

                # Get next batch of run IDs
                run_ids = list(base_query.values_list("pk", flat=True)[:batch_size])
                if not run_ids:
                    break

                with transaction.atomic():
                    deleted_count, _ = WorkflowRun.objects.filter(pk__in=run_ids).delete()
                    total_deleted += deleted_count
                    self.stdout.write(
                        f"  Deleted batch {batches_processed + 1}: {deleted_count} workflows"
                    )

                batches_processed += 1
        else:
            # Dry-run: just count and report what would be deleted
            total_deleted = total_purgeable
            batches_processed = (
                (total_purgeable + batch_size - 1) // batch_size if total_purgeable > 0 else 0
            )
            self.stdout.write(
                f"  Would delete {total_deleted} workflows in {batches_processed} batches"
            )

        self.stdout.write(f"\n{prefix}Purge complete: {total_deleted} workflows.")
