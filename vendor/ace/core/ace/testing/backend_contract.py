"""Shared backend contract tests for adapter implementations.

Each adapter (Django, DBOS, Temporal) should run these contract tests to verify
correct behavior. The tests are designed to be inherited and customized.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ace.backend import BackendCapabilities, BackendName
from ace.commands import (
    ActivityExecutionMode,
    CompleteWorkflow,
    FailWorkflow,
    ScheduleActivity,
    ScheduleTimer,
    WorkflowTransition,
)
from ace.models import (
    WorkflowContext,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowFailure,
    WorkflowStatus,
)

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from ace.definitions import WorkflowDefinition
    from ace.json_types import JsonObject


class SimpleWorkflow:
    """A simple workflow for contract testing."""

    name = "simple_test_workflow"
    version = "1"

    def start(self, input: JsonObject, context: WorkflowContext) -> WorkflowTransition:
        return WorkflowTransition(
            state={"step": "started", "input": input},
            commands=(
                ScheduleActivity(
                    activity_key="activity_1",
                    activity_name="test_activity",
                    input=input,
                ),
            ),
        )

    def advance(
        self,
        state: JsonObject,
        event: WorkflowEvent,
        context: WorkflowContext,
    ) -> WorkflowTransition:
        if event.event_type == WorkflowEventType.ACTIVITY_COMPLETED:
            return WorkflowTransition(
                state={"step": "completed", "result": event.payload.get("result")},
                commands=(CompleteWorkflow(result=event.payload.get("result")),),
            )
        if event.event_type == WorkflowEventType.ACTIVITY_FAILED:
            return WorkflowTransition(
                state={"step": "failed"},
                commands=(
                    FailWorkflow(
                        failure=WorkflowFailure(
                            error_type=event.payload.get("error_type", "ActivityFailed"),
                            message=event.payload.get("message", "Activity failed."),
                        )
                    ),
                ),
            )
        return WorkflowTransition(state=state)


class BackendContractTest(ABC):
    """Abstract base class for backend contract tests.

    Subclass this and implement the abstract methods to test a specific backend.
    """

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities:
        """Return the capabilities of the backend being tested."""
        ...

    @property
    @abstractmethod
    def backend_name(self) -> BackendName:
        """Return the name of the backend being tested."""
        ...

    @abstractmethod
    def create_workflow_run(
        self,
        workflow_name: str,
        workflow_version: str,
        input: JsonObject,
    ) -> str:
        """Create a workflow run and return its ID."""
        ...

    @abstractmethod
    def get_workflow_status(self, run_id: str) -> WorkflowStatus:
        """Get the current status of a workflow run."""
        ...

    @abstractmethod
    def complete_activity(
        self,
        run_id: str,
        activity_key: str,
        result: object,
    ) -> None:
        """Complete an activity with the given result."""
        ...

    @abstractmethod
    def fail_activity(
        self,
        run_id: str,
        activity_key: str,
        error_type: str,
        message: str,
    ) -> None:
        """Fail an activity with the given error."""
        ...

    # Contract test methods

    def test_workflow_start_creates_pending_activity(self) -> None:
        """Starting a workflow should create activities from emitted commands."""
        run_id = self.create_workflow_run(
            "simple_test_workflow",
            "1",
            {"value": 42},
        )
        status = self.get_workflow_status(run_id)
        assert status in (WorkflowStatus.RUNNING, WorkflowStatus.WAITING, WorkflowStatus.PENDING)

    def test_activity_completion_advances_workflow(self) -> None:
        """Completing an activity should trigger workflow transition."""
        run_id = self.create_workflow_run(
            "simple_test_workflow",
            "1",
            {"value": 42},
        )
        self.complete_activity(run_id, "activity_1", {"computed": 84})
        status = self.get_workflow_status(run_id)
        assert status == WorkflowStatus.COMPLETED

    def test_authority_isolation(self) -> None:
        """A backend should only accept runs it owns."""
        # This test verifies that run IDs from other backends are rejected
        # Implementations should override to test specific isolation behavior
        pass

    def test_unsupported_execution_mode_rejected(self) -> None:
        """Commands with unsupported execution modes should be rejected."""
        if ActivityExecutionMode.TRANSACTIONAL.value in self.capabilities.supported_execution_modes:
            return  # Skip if transactional is supported

        # Attempting to use TRANSACTIONAL mode on a backend that doesn't support it
        # should raise an error during command validation
        pass
