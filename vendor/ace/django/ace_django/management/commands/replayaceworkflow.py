"""Replay a workflow run to verify determinism.

This command re-executes a workflow definition against its persisted history
and reports whether the replay matches the expected state and commands.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from ace.replay import ReplayStatus
from ace_django.replay import replay_workflow


class Command(BaseCommand):
    help = "Replay a workflow run to verify determinism."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "run_id",
            help="The workflow run ID (UUID) to replay.",
        )
        parser.add_argument(
            "--registry",
            default="ace_django.registry.registry",
            help="Python path to the workflow registry (default: ace_django.registry.registry).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output result as JSON.",
        )
        parser.add_argument(
            "--verbose",
            "-v",
            action="count",
            default=0,
            help="Increase output verbosity.",
        )

    def handle(self, *args, **options) -> None:
        run_id = options["run_id"]
        registry_path = options["registry"]
        output_json = options["json"]
        verbose = options["verbose"]

        # Import the registry
        try:
            module_path, attr_name = registry_path.rsplit(".", 1)
            module = __import__(module_path, fromlist=[attr_name])
            registry = getattr(module, attr_name)
        except (ImportError, AttributeError, ValueError) as e:
            raise CommandError(f"Could not import registry from '{registry_path}': {e}")

        # Replay the workflow
        try:
            result = replay_workflow(run_id, registry)
        except Exception as e:
            if output_json:
                self.stdout.write(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "error": str(e),
                            "status": "ERROR",
                        }
                    )
                )
            raise CommandError(f"Failed to replay workflow: {e}")

        # Output results
        if output_json:
            output = {
                "run_id": result.run_id,
                "workflow_name": result.workflow_name,
                "workflow_version": result.workflow_version,
                "status": result.report.status.value,
                "definition_found": result.definition_found,
                "events_loaded": result.events_loaded,
                "commands_loaded": result.commands_loaded,
                "message": result.report.message,
            }
            if verbose > 0:
                output["expected_status"] = result.report.expected_status.value
                output["replayed_status"] = (
                    result.report.replayed_status.value if result.report.replayed_status else None
                )
                output["events"] = [
                    {
                        "sequence": e.sequence,
                        "event_type": e.event_type,
                        "status": e.status.value,
                        "commands_available": e.commands_available,
                        "message": e.message,
                    }
                    for e in result.report.events
                ]
            self.stdout.write(json.dumps(output, indent=2))
        else:
            status = result.report.status
            if status == ReplayStatus.PASSED:
                self.stdout.write(self.style.SUCCESS(f"PASSED: {run_id}"))
                self.stdout.write(f"  Workflow: {result.workflow_name}:{result.workflow_version}")
                self.stdout.write(
                    f"  Events: {result.events_loaded}, Commands: {result.commands_loaded}"
                )
            elif status == ReplayStatus.PARTIAL:
                self.stdout.write(self.style.WARNING(f"PARTIAL: {run_id}"))
                self.stdout.write(f"  Workflow: {result.workflow_name}:{result.workflow_version}")
                self.stdout.write(f"  State verified, but some events lack persisted commands.")
                self.stdout.write(
                    f"  Events: {result.events_loaded}, Commands: {result.commands_loaded}"
                )
            elif status == ReplayStatus.FAILED:
                self.stdout.write(self.style.ERROR(f"FAILED: {run_id}"))
                self.stdout.write(f"  Workflow: {result.workflow_name}:{result.workflow_version}")
                self.stdout.write(f"  {result.report.message}")
                if verbose > 0:
                    for e in result.report.events:
                        if e.status == ReplayStatus.FAILED:
                            self.stdout.write(
                                f"    Event {e.sequence} ({e.event_type}): {e.message}"
                            )
            else:  # ERROR
                self.stdout.write(self.style.ERROR(f"ERROR: {run_id}"))
                self.stdout.write(f"  {result.report.message}")

        # Exit code
        if result.report.status in (ReplayStatus.FAILED, ReplayStatus.ERROR):
            raise SystemExit(1)
