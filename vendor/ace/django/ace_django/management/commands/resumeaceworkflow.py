"""Resume a blocked workflow by resetting its state for retry."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from ace_django.models import InboxStatus, WorkflowInboxEvent, WorkflowRun
from ace import WorkflowStatus


class Command(BaseCommand):
    help = "Resume a blocked workflow by restoring its previous state and retrying."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "run_id",
            help="Workflow run ID to resume",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Skip confirmation prompt",
        )

    def handle(self, *args, **options) -> None:
        run_id = options["run_id"]
        dry_run = options["dry_run"]
        force = options["force"]

        try:
            run = WorkflowRun.objects.get(pk=run_id)
        except WorkflowRun.DoesNotExist:
            raise CommandError(f"Workflow run {run_id} not found")

        # Validate run is blocked
        status = WorkflowStatus(run.status)
        if status != WorkflowStatus.BLOCKED:
            raise CommandError(f"Workflow {run_id} is not blocked (status={status.value})")

        if not run.blocked_from_status:
            raise CommandError(
                f"Workflow {run_id} has no blocked_from_status - cannot determine restore state"
            )

        # Find dead-lettered inbox events
        dead_events = list(
            WorkflowInboxEvent.objects.filter(
                workflow_run=run,
                status=InboxStatus.DEAD_LETTER,
            ).order_by("inbox_sequence")
        )

        # Also find pending events that were queued after blocking
        pending_events = list(
            WorkflowInboxEvent.objects.filter(
                workflow_run=run,
                status__in=[InboxStatus.PENDING, InboxStatus.RETRYING],
            ).order_by("inbox_sequence")
        )

        self.stdout.write(f"\nWorkflow: {run_id}")
        self.stdout.write(f"  Current status: {run.status}")
        self.stdout.write(f"  Blocked from: {run.blocked_from_status}")
        self.stdout.write(f"  Block reason: {run.block_reason or 'N/A'}")
        self.stdout.write(f"  Dead-lettered events: {len(dead_events)}")
        self.stdout.write(f"  Pending events: {len(pending_events)}")

        if dead_events:
            self.stdout.write("\nDead-lettered events:")
            for event in dead_events[:10]:  # Show first 10
                self.stdout.write(
                    f"  - seq {event.inbox_sequence}: {event.source_type}:{event.source_key}"
                )
            if len(dead_events) > 10:
                self.stdout.write(f"  ... and {len(dead_events) - 10} more")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] Would resume workflow"))
            return

        if not force:
            self.stdout.write("\nThis will:")
            self.stdout.write(f"  1. Restore status from BLOCKED to {run.blocked_from_status}")
            self.stdout.write("  2. Reset all dead-lettered events for retry")
            self.stdout.write("  3. Clear blocking metadata")
            confirm = input("\nProceed? [y/N]: ")
            if confirm.lower() != "y":
                self.stdout.write("Aborted")
                return

        # Resume the workflow
        now = timezone.now()

        with transaction.atomic():
            # Lock workflow
            locked_run = WorkflowRun.objects.select_for_update().get(pk=run_id)

            # Verify still blocked
            if locked_run.status != WorkflowStatus.BLOCKED.value:
                raise CommandError("Workflow status changed - aborting")

            # Restore status
            locked_run.status = locked_run.blocked_from_status
            locked_run.blocked_at = None
            locked_run.blocked_from_status = None
            locked_run.block_reason = None
            locked_run.save(
                update_fields=[
                    "status",
                    "blocked_at",
                    "blocked_from_status",
                    "block_reason",
                    "updated_at",
                ]
            )

            # Reset dead-lettered events
            reset_count = WorkflowInboxEvent.objects.filter(
                workflow_run=run,
                status=InboxStatus.DEAD_LETTER,
            ).update(
                status=InboxStatus.RETRYING,
                available_at=now,
                attempts=0,
                updated_at=now,
            )

        self.stdout.write(self.style.SUCCESS(f"\nResumed workflow {run_id}"))
        self.stdout.write(f"  Status restored to: {locked_run.status}")
        self.stdout.write(f"  Events reset: {reset_count}")
