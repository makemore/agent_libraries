"""Tests for canonical command serialization and deserialization."""

from datetime import datetime, timezone

import pytest
from ace import (
    ActivityExecutionMode,
    ActivityGroupCompletionPolicy,
    ActivityTimeoutConfig,
    CancelWorkflow,
    CompleteWorkflow,
    FailWorkflow,
    RetryPolicy,
    ScheduleActivity,
    ScheduleActivityGroup,
    ScheduleTimer,
    WorkflowFailure,
    deserialize_command,
    deserialize_commands,
    serialize_command,
    serialize_commands,
    serialize_commands_json,
)


class TestSerializeCommand:
    def test_schedule_activity_round_trip(self):
        cmd = ScheduleActivity(
            activity_key="test_key",
            activity_name="test_activity",
            input={"value": 42},
            activity_version="2",
            queue="high",
            retry_policy=RetryPolicy(max_attempts=5),
            idempotency_key="idem-123",
            priority=10,
            delay_seconds=30.0,
            partition_key="tenant-1",
            execution_mode=ActivityExecutionMode.TRANSACTIONAL,
            timeout=ActivityTimeoutConfig(
                schedule_to_close_seconds=3600.0,
                start_to_close_seconds=300.0,
                heartbeat_seconds=60.0,
            ),
        )
        serialized = serialize_command(cmd)
        deserialized = deserialize_command(serialized)

        assert deserialized.activity_key == cmd.activity_key
        assert deserialized.activity_name == cmd.activity_name
        assert deserialized.input == cmd.input
        assert deserialized.priority == cmd.priority
        assert deserialized.execution_mode == cmd.execution_mode
        assert deserialized.timeout.schedule_to_close_seconds == 3600.0

    def test_schedule_activity_group_round_trip(self):
        group = ScheduleActivityGroup(
            group_key="batch_1",
            activities=(
                ScheduleActivity(activity_key="a1", activity_name="task1"),
                ScheduleActivity(activity_key="a2", activity_name="task2"),
            ),
            completion_policy=ActivityGroupCompletionPolicy.ALL_SUCCESS,
        )
        serialized = serialize_command(group)
        deserialized = deserialize_command(serialized)

        assert deserialized.group_key == group.group_key
        assert len(deserialized.activities) == 2
        assert deserialized.activities[0].activity_key == "a1"

    def test_schedule_timer_round_trip(self):
        fire_at = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        cmd = ScheduleTimer(
            timer_key="reminder",
            fire_at=fire_at,
            payload={"message": "Wake up"},
        )
        serialized = serialize_command(cmd)
        deserialized = deserialize_command(serialized)

        assert deserialized.timer_key == cmd.timer_key
        assert deserialized.fire_at == fire_at
        assert deserialized.payload == cmd.payload

    def test_complete_workflow_round_trip(self):
        cmd = CompleteWorkflow(result={"output": [1, 2, 3]})
        serialized = serialize_command(cmd)
        deserialized = deserialize_command(serialized)

        assert deserialized.result == cmd.result

    def test_fail_workflow_round_trip(self):
        failure = WorkflowFailure(
            error_type="ValidationError",
            message="Invalid input",
            details={"field": "email"},
        )
        cmd = FailWorkflow(failure=failure)
        serialized = serialize_command(cmd)
        deserialized = deserialize_command(serialized)

        assert deserialized.failure.error_type == failure.error_type
        assert deserialized.failure.message == failure.message

    def test_cancel_workflow_round_trip(self):
        cmd = CancelWorkflow(reason="User requested cancellation")
        serialized = serialize_command(cmd)
        deserialized = deserialize_command(serialized)

        assert deserialized.reason == cmd.reason


class TestSerializeCommands:
    def test_multiple_commands_round_trip(self):
        commands = (
            ScheduleActivity(activity_key="a1", activity_name="task1"),
            ScheduleActivity(activity_key="a2", activity_name="task2"),
        )
        serialized = serialize_commands(commands)
        deserialized = deserialize_commands(serialized)

        assert len(deserialized) == 2
        assert deserialized[0].activity_key == "a1"
        assert deserialized[1].activity_key == "a2"

    def test_json_serialization_is_deterministic(self):
        commands = (
            ScheduleActivity(activity_key="a1", activity_name="task1"),
            ScheduleActivity(activity_key="a2", activity_name="task2"),
        )
        json1 = serialize_commands_json(commands)
        json2 = serialize_commands_json(commands)

        assert json1 == json2


class TestInvalidCommand:
    def test_unknown_command_type_raises(self):
        with pytest.raises(ValueError, match="Unknown command type"):
            deserialize_command({"type": "UnknownCommand"})
