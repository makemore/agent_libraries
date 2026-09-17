"""Migration tests — verify 0001 → 0002 preserves data and 0002 supports groups."""

from datetime import UTC, datetime

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

FROZEN_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@pytest.mark.django_db(transaction=True)
class TestMigration0002:
    """Forward migration preserves existing data; groups work after migration."""

    app_label = "ace_django"
    migrate_from = ("ace_django", "0001_initial")
    migrate_to = ("ace_django", "0002_activitygrouprun_activityrun_group_and_more")
    migrate_latest = ("ace_django", "0005_durable_constraints_and_indexes")

    def _apply(self, target: tuple[str, str]) -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        executor.loader.build_graph()
        return executor

    def test_forward_preserves_existing_data_and_supports_groups(self) -> None:
        # --- Migrate to 0001 and create pre-existing data. ---
        executor = self._apply(self.migrate_from)
        old_apps = executor.loader.project_state([self.migrate_from]).apps

        WorkflowRun = old_apps.get_model(self.app_label, "WorkflowRun")
        ActivityRun = old_apps.get_model(self.app_label, "ActivityRun")

        wf = WorkflowRun.objects.create(
            workflow_name="test-wf",
            workflow_version="1",
            status="RUNNING",
            started_at=FROZEN_NOW,
        )
        act = ActivityRun.objects.create(
            workflow_run=wf,
            activity_key="step-1",
            activity_name="test.step",
            status="READY",
        )

        # --- Forward to 0002. ---
        executor = self._apply(self.migrate_to)
        new_apps = executor.loader.project_state([self.migrate_to]).apps

        NewWorkflowRun = new_apps.get_model(self.app_label, "WorkflowRun")
        NewActivityRun = new_apps.get_model(self.app_label, "ActivityRun")
        ActivityGroupRun = new_apps.get_model(self.app_label, "ActivityGroupRun")

        # Pre-existing workflow/activity preserved with null group.
        preserved_wf = NewWorkflowRun.objects.get(pk=wf.pk)
        assert preserved_wf.workflow_name == "test-wf"

        preserved_act = NewActivityRun.objects.get(pk=act.pk)
        assert preserved_act.activity_key == "step-1"
        assert preserved_act.group_id is None

        # Create a group after migration.
        grp = ActivityGroupRun.objects.create(
            workflow_run=preserved_wf,
            group_key="parallel-extract",
            completion_policy="ALL_SUCCESS",
            status="RUNNING",
        )
        member = NewActivityRun.objects.create(
            workflow_run=preserved_wf,
            group=grp,
            activity_key="extract-a",
            activity_name="test.extract",
            status="READY",
        )
        assert member.group_id == grp.pk
        assert grp.members.count() == 1

        # Restore to latest migration for subsequent tests
        self._apply(self.migrate_latest)

    def test_reverse_drops_group_tables_cleanly(self) -> None:
        """Reverse migration removes group tables without affecting workflows."""
        # Forward first.
        self._apply(self.migrate_to)

        try:
            # Reverse back.
            executor = self._apply(self.migrate_from)
            old_apps = executor.loader.project_state([self.migrate_from]).apps

            # ActivityGroupRun should no longer be available.
            with pytest.raises(LookupError):
                old_apps.get_model(self.app_label, "ActivityGroupRun")

            # ActivityRun still exists without group field.
            ActivityRun = old_apps.get_model(self.app_label, "ActivityRun")
            field_names = {f.name for f in ActivityRun._meta.get_fields()}
            assert "group" not in field_names
        finally:
            # Migration tests share the test database with randomized modules.
            # Always restore the current schema before the next test starts.
            self._apply(self.migrate_latest)
