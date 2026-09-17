"""Backend contract tests for the DBOS adapter."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from ace.backend import DBOS_CAPABILITIES, BackendCapabilities, BackendName
from ace.codec import deserialize_command, serialize_command
from ace.commands import (
    ActivityExecutionMode,
    ActivityTimeoutConfig,
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
)
from ace.json_types import JsonObject
from ace.models import WorkflowFailure, WorkflowStatus
from ace.registry import WorkflowRegistry
from ace.testing.backend_contract import BackendContractTest, SimpleWorkflow

import ace_dbos
from ace_dbos.adapter import AceDbosAdapter, ForeignRunError
from ace_dbos.codec import DbosAction, UnsupportedCommand, translate_command


class TestDbosContract(BackendContractTest):
    def setup_method(self) -> None:
        registry = WorkflowRegistry()
        registry.register(SimpleWorkflow())
        self.adapter = AceDbosAdapter(registry)

    @property
    def capabilities(self) -> BackendCapabilities:
        return self.adapter.capabilities

    @property
    def backend_name(self) -> BackendName:
        return self.adapter.backend_name

    def create_workflow_run(
        self,
        workflow_name: str,
        workflow_version: str,
        input: JsonObject,
    ) -> str:
        return self.adapter.create_workflow_run(workflow_name, workflow_version, input)

    def get_workflow_status(self, run_id: str) -> WorkflowStatus:
        return self.adapter.get_workflow_status(run_id)

    def complete_activity(self, run_id: str, activity_key: str, result: object) -> None:
        self.adapter.complete_activity(run_id, activity_key, result)

    def fail_activity(
        self,
        run_id: str,
        activity_key: str,
        error_type: str,
        message: str,
    ) -> None:
        self.adapter.fail_activity(run_id, activity_key, error_type, message)

    def test_authority_isolation(self) -> None:
        for foreign_id in (str(uuid4()), "temporal-xyz"):
            with pytest.raises(ForeignRunError):
                self.adapter.get_workflow_status(foreign_id)
            with pytest.raises(ForeignRunError):
                self.adapter.complete_activity(foreign_id, "activity_1", None)


class TestUnsupportedCapabilities:
    def test_transactional_mode_rejected(self) -> None:
        command = ScheduleActivity(
            activity_key="a",
            activity_name="work",
            execution_mode=ActivityExecutionMode.TRANSACTIONAL,
        )
        with pytest.raises(UnsupportedCommand):
            translate_command(command, DBOS_CAPABILITIES)

    def test_priority_rejected(self) -> None:
        command = ScheduleActivity(activity_key="a", activity_name="work", priority=5)
        with pytest.raises(UnsupportedCommand):
            translate_command(command, DBOS_CAPABILITIES)

    def test_delay_rejected(self) -> None:
        command = ScheduleActivity(activity_key="a", activity_name="work", delay_seconds=1.5)
        with pytest.raises(UnsupportedCommand):
            translate_command(command, DBOS_CAPABILITIES)

    def test_partition_key_rejected(self) -> None:
        command = ScheduleActivity(activity_key="a", activity_name="work", partition_key="p1")
        with pytest.raises(UnsupportedCommand):
            translate_command(command, DBOS_CAPABILITIES)

    def test_heartbeat_timeout_rejected(self) -> None:
        command = ScheduleActivity(
            activity_key="a",
            activity_name="work",
            timeout=ActivityTimeoutConfig(start_to_close_seconds=30.0, heartbeat_seconds=5.0),
        )
        with pytest.raises(UnsupportedCommand):
            translate_command(command, DBOS_CAPABILITIES)

    def test_activity_group_rejected(self) -> None:
        group = ScheduleActivityGroup(
            group_key="g",
            activities=(ScheduleActivity(activity_key="a", activity_name="work"),),
        )
        with pytest.raises(UnsupportedCommand):
            translate_command(group, DBOS_CAPABILITIES)


class TestPlanSerialization:
    def test_supported_commands_round_trip(self) -> None:
        fire_at = datetime.now(UTC) + timedelta(seconds=60)
        cases = (
            (
                ScheduleActivity(activity_key="a", activity_name="work", input={"x": 1}),
                DbosAction.ENQUEUE_STEP,
            ),
            (ScheduleTimer(timer_key="t", fire_at=fire_at), DbosAction.START_TIMER),
            (CompleteWorkflow(result={"ok": True}), DbosAction.COMPLETE_WORKFLOW),
            (
                FailWorkflow(failure=WorkflowFailure(error_type="Boom", message="bad")),
                DbosAction.FAIL_WORKFLOW,
            ),
            (CancelWorkflow(reason="stop"), DbosAction.CANCEL_WORKFLOW),
        )
        for command, action in cases:
            plan = translate_command(command, DBOS_CAPABILITIES)
            assert plan.action is action
            assert deserialize_command(serialize_command(plan.command)) == command


class TestImportBoundary:
    def test_ace_dbos_never_imports_django(self) -> None:
        assert "django" not in sys.modules
        assert "ace_django" not in sys.modules
        package_dir = Path(ace_dbos.__file__).resolve().parent
        for source in sorted(package_dir.rglob("*.py")):
            text = source.read_text()
            assert "import django" not in text, source
            assert "from django" not in text, source
            assert "ace_django" not in text, source
