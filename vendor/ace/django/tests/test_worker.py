from datetime import timedelta
from threading import Event

import pytest
from ace import ActivityContext, ActivityRegistry, WorkflowStatus
from ace.json_types import JsonObject, JsonValue
from ace_django.exceptions import WorkerConfigurationError
from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    WorkerStatus,
    WorkflowRun,
)
from ace_django.tests.helpers import FROZEN_NOW
from ace_django.worker import AceWorker, WorkerRuntime, load_runtime
from django.test import override_settings

pytestmark = pytest.mark.django_db


class ExpectedActivityError(Exception):
    pass


def _activity(name: str, *, retry_policy: dict | None = None) -> ActivityRun:
    return ActivityRun.objects.create(
        id="30000000-0000-0000-0000-000000000021",
        activity_key="worker-test",
        activity_name=name,
        input={"subject": "risk-1"},
        retry_policy=retry_policy or {"max_attempts": 1},
        available_at=FROZEN_NOW,
    )


def _worker(name: str, function) -> AceWorker:
    activities = ActivityRegistry()
    activities.register(name, function)
    return AceWorker(
        WorkerRuntime(activities),
        worker_id="worker-test",
        lease_duration=timedelta(seconds=5),
        renewal_interval=timedelta(seconds=1),
        now=lambda: FROZEN_NOW,
    )


def test_worker_executes_activity_and_persists_heartbeat_details() -> None:
    activity = _activity("tests.success")

    def succeed(context: ActivityContext, input: JsonObject) -> JsonValue:
        context.heartbeat({"phase": "executing"})
        return {"subject": input["subject"], "done": True}

    assert _worker("tests.success", succeed).run_once() is True

    activity.refresh_from_db()
    assert activity.status == ActivityStatus.SUCCEEDED
    assert activity.result == {"subject": "risk-1", "done": True}
    assert activity.attempts.get().details == {"phase": "executing"}


def test_worker_records_terminal_activity_failure() -> None:
    activity = _activity("tests.failure")

    def fail(context: ActivityContext, input: JsonObject) -> JsonValue:
        raise ExpectedActivityError("expected failure")

    assert _worker("tests.failure", fail).run_once() is True

    activity.refresh_from_db()
    assert activity.status == ActivityStatus.FAILED
    assert activity.failure is not None
    assert activity.failure["error_type"] == "ExpectedActivityError"
    assert activity.attempts.get().status == AttemptStatus.FAILED


def test_worker_cooperatively_cancels_running_activity() -> None:
    run = WorkflowRun.objects.create(
        id="10000000-0000-0000-0000-000000000021",
        workflow_name="worker-test",
        workflow_version="1",
        status=WorkflowStatus.RUNNING,
    )
    activity = _activity("tests.cancel")
    activity.workflow_run = run
    activity.save(update_fields=["workflow_run", "updated_at"])

    def cancel(context: ActivityContext, input: JsonObject) -> JsonValue:
        WorkflowRun.objects.filter(pk=run.id).update(cancel_requested_at=FROZEN_NOW)
        context.check_cancelled()
        return None

    assert _worker("tests.cancel", cancel).run_once() is True

    activity.refresh_from_db()
    assert activity.status == ActivityStatus.CANCELLED
    assert activity.attempts.get().status == AttemptStatus.CANCELLED


def test_worker_run_records_stopped_heartbeat_when_already_draining() -> None:
    stop = Event()
    stop.set()
    worker = AceWorker(
        WorkerRuntime(ActivityRegistry()),
        worker_id="worker-stopped",
        stop_signal=stop,
        now=lambda: FROZEN_NOW,
    )

    worker.run()

    heartbeat = AceWorkerHeartbeat.objects.get(worker_id="worker-stopped")
    assert heartbeat.status == WorkerStatus.STOPPED
    assert heartbeat.last_seen_at == FROZEN_NOW


def test_runtime_factory_is_explicit_and_validated() -> None:
    with (
        override_settings(ACE_RUNTIME_FACTORY=None),
        pytest.raises(WorkerConfigurationError, match="ACE_RUNTIME_FACTORY"),
    ):
        load_runtime()

    runtime = load_runtime()
    assert runtime.activities.resolve("tests.echo") is not None
