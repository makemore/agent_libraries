"""Synchronize ACE queue configurations from settings.

This command creates or updates QueueConfig rows from ACE_QUEUES settings.
It does NOT delete queues not in settings (for safety), but can disable them.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ace_django.models import QueueConfig


class Command(BaseCommand):
    help = "Synchronize ACE queue configurations from settings."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes",
        )
        parser.add_argument(
            "--disable-unlisted",
            action="store_true",
            help="Disable queues not in settings (does not delete)",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        disable_unlisted = options["disable_unlisted"]

        # Get queue configurations from settings
        queue_configs = getattr(settings, "ACE_QUEUES", None)
        if queue_configs is None:
            # Default queue configuration
            queue_configs = {
                "medium": {
                    "enabled": True,
                    "global_concurrency": None,
                    "rate_limit_count": None,
                    "rate_limit_period_seconds": None,
                    "partition_concurrency": None,
                },
            }

        if not isinstance(queue_configs, dict):
            raise CommandError("ACE_QUEUES must be a dictionary")

        created = 0
        updated = 0
        disabled = 0

        # Process each queue in settings
        for name, config in queue_configs.items():
            if not isinstance(config, dict):
                raise CommandError(f"Queue config for '{name}' must be a dictionary")

            # Check if queue already exists
            try:
                queue = QueueConfig.objects.get(name=name)
                was_created = False
            except QueueConfig.DoesNotExist:
                if dry_run:
                    self.stdout.write(f"  Would create queue: {name}")
                    created += 1
                    continue
                else:
                    queue = QueueConfig.objects.create(
                        name=name,
                        enabled=config.get("enabled", True),
                        global_concurrency=config.get("global_concurrency"),
                        rate_limit_count=config.get("rate_limit_count"),
                        rate_limit_period_seconds=config.get("rate_limit_period_seconds"),
                        partition_concurrency=config.get("partition_concurrency"),
                    )
                    self.stdout.write(self.style.SUCCESS(f"  Created queue: {name}"))
                    created += 1
                    continue

            # Queue exists - check if update needed
            needs_update = (
                queue.enabled != config.get("enabled", True)
                or queue.global_concurrency != config.get("global_concurrency")
                or queue.rate_limit_count != config.get("rate_limit_count")
                or queue.rate_limit_period_seconds != config.get("rate_limit_period_seconds")
                or queue.partition_concurrency != config.get("partition_concurrency")
            )

            if needs_update:
                if not dry_run:
                    queue.enabled = config.get("enabled", True)
                    queue.global_concurrency = config.get("global_concurrency")
                    queue.rate_limit_count = config.get("rate_limit_count")
                    queue.rate_limit_period_seconds = config.get("rate_limit_period_seconds")
                    queue.partition_concurrency = config.get("partition_concurrency")
                    queue.save()
                    self.stdout.write(f"  Updated queue: {name}")
                else:
                    self.stdout.write(f"  Would update queue: {name}")
                updated += 1

        # Optionally disable unlisted queues
        if disable_unlisted:
            unlisted = QueueConfig.objects.filter(enabled=True).exclude(
                name__in=queue_configs.keys()
            )
            for queue in unlisted:
                if not dry_run:
                    queue.enabled = False
                    queue.save(update_fields=["enabled", "updated_at"])
                    self.stdout.write(self.style.WARNING(f"  Disabled queue: {queue.name}"))
                else:
                    self.stdout.write(f"  Would disable queue: {queue.name}")
                disabled += 1

        # Summary
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            f"\n{prefix}Queue sync complete: "
            f"{created} created, {updated} updated, {disabled} disabled"
        )
