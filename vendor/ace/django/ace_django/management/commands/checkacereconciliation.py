"""Report ACE activity group reconciliation without mutating execution state."""

from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from ace_django.health import collect_activity_group_reconciliation


class Command(BaseCommand):
    help = "Check ACE activity group state consistency."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--fail-on-inconsistency", action="store_true")
        parser.add_argument(
            "--terminal-window-seconds",
            type=int,
            default=None,
            help=(
                "Only scan terminal groups updated within this window. "
                "RUNNING groups are always scanned in full."
            ),
        )

    def handle(self, *args, **options) -> None:
        window_seconds = options["terminal_window_seconds"]
        terminal_window = timedelta(seconds=window_seconds) if window_seconds is not None else None
        report = collect_activity_group_reconciliation(terminal_window=terminal_window)
        output = {
            "checked_at": report.checked_at.isoformat(),
            "consistent": report.consistent,
            "inconsistencies": [
                {
                    "group_id": issue.group_id,
                    "group_key": issue.group_key,
                    "reason": issue.reason,
                }
                for issue in report.inconsistencies
            ],
        }
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True))
        if options["fail_on_inconsistency"] and not report.consistent:
            raise CommandError(
                f"ACE group reconciliation found {len(report.inconsistencies)} inconsistencies."
            )
