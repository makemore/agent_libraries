"""Backend contract tests for the Temporal adapter."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ace.backend import TEMPORAL_CAPABILITIES, BackendCapabilities, BackendName
from ace.codec import deserialize_command, serialize_command
from ace.commands import (
    ActivityExecutionMode,
    ActivityTimeoutConfig,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
)
from ace.json_types import JsonObject
from ace.models import WorkflowStatus
from ace.registry import WorkflowRegistry
from ace.testing.backend_contract import BackendContractTest, SimpleWorkflow

from ace_temporal import (
    AceTemporalAdapter,
    ForeignRunError,
    TemporalActionKind,
    UnsupportedCommand,
    translate_command,
)


class TestTemporalContract(BackendContractTest):
    def setup_method(self) -> None:
        registry = WorkflowRegistry()
        registry.register(SimpleWorkflow())
        self.adapter = AceTemporalAdapter(registry)

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
        self.create_workflow_run("simple_test_workflow", "1", {"value": 1})
        django_style_id = str(uuid4())
        with pytest.raises(ForeignRunError):
            self.adapter.get_workflow_status(django_style_id)
        with pytest.raises(ForeignRunError):
            self.adapter.complete_activity("dbos-xyz", "activity_1", {"ok": True})

    def test_transactional_mode_rejected(self) -> None:
        command = ScheduleActivity(
            activity_key="a",
            activity_name="n",
            execution_mode=ActivityExecutionMode.TRANSACTIONAL,
        )
        with pytest.raises(UnsupportedCommand):
            translate_command(command, TEMPORAL_CAPABILITIES)

    def test_priority_rejected(self) -> None:
        command = ScheduleActivity(activity_key="a", activity_name="n", priority=5)
        with pytest.raises(UnsupportedCommand):
            translate_command(command, TEMPORAL_CAPABILITIES)

    def test_delay_rejected(self) -> None:
        command = ScheduleActivity(activity_key="a", activity_name="n", delay_seconds=1.5)
        with pytest.raises(UnsupportedCommand):
            translate_command(command, TEMPORAL_CAPABILITIES)

    def test_partition_key_rejected(self) -> None:
        command = ScheduleActivity(activity_key="a", activity_name="n", partition_key="p")
        with pytest.raises(UnsupportedCommand):
            translate_command(command, TEMPORAL_CAPABILITIES)

    def test_activity_group_rejected(self) -> None:
        group = ScheduleActivityGroup(
            group_key="g",
            activities=(ScheduleActivity(activity_key="a", activity_name="n"),),
        )
        with pytest.raises(UnsupportedCommand):
            translate_command(group, TEMPORAL_CAPABILITIES)

    def test_heartbeat_timeout_accepted(self) -> None:
        command = ScheduleActivity(
            activity_key="a",
            activity_name="n",
            timeout=ActivityTimeoutConfig(
                schedule_to_close_seconds=120.0,
                start_to_close_seconds=60.0,
                heartbeat_seconds=10.0,
            ),
        )
        plan = translate_command(command, TEMPORAL_CAPABILITIES)
        assert plan.kind == TemporalActionKind.EXECUTE_ACTIVITY
        assert plan.schedule_to_close_seconds == 120.0
        assert plan.start_to_close_seconds == 60.0
        assert plan.heartbeat_seconds == 10.0

    def test_command_serialization_round_trip(self) -> None:
        activity = ScheduleActivity(
            activity_key="a",
            activity_name="n",
            input={"value": 1},
            timeout=ActivityTimeoutConfig(start_to_close_seconds=30.0, heartbeat_seconds=5.0),
        )
        timer = ScheduleTimer(timer_key="t", fire_at=datetime(2026, 1, 1, tzinfo=UTC))
        assert deserialize_command(serialize_command(activity)) == activity
        assert deserialize_command(serialize_command(timer)) == timer


def test_import_boundary() -> None:
    import ace_temporal
    import ace_temporal.adapter
    import ace_temporal.codec

    assert "django" not in sys.modules
    assert "ace_django" not in sys.modules
    forbidden = re.compile(r"^\s*(import|from)\s+(django|ace_django)\b")
    top_level_sdk = re.compile(r"^(import|from)\s+temporalio\b")
    for source in Path(ace_temporal.__file__).parent.glob("*.py"):
        for line in source.read_text().splitlines():
            assert not forbidden.match(line), f"{source.name}: {line!r}"
            assert not top_level_sdk.match(line), f"{source.name}: {line!r}"
