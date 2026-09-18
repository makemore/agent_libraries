from datetime import timedelta
from threading import Event

import pytest
from ace import ActivityContext, ActivityRegistry, WorkflowEventType, WorkflowStatus
from ace.json_types import JsonObject, JsonValue
from ace_django.exceptions import WorkerConfigurationError
from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityRun,
    ActivityStatus,
    AttemptStatus,
    InboxStatus,
    WorkerStatus,
    WorkflowInboxEvent,
    WorkflowRun,
)
from ace_django.runtime import DjangoRuntime
from ace_django.tests.helpers import FROZEN_NOW, build_engine
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


@pytest.mark.parametrize("fails", [False, True], ids=["success", "terminal-failure"])
@pytest.mark.parametrize(
    "legacy_direct_engine",
    [
        pytest.param(False, id="django-runtime"),
        pytest.param(True, id="legacy-direct-engine-compatibility"),
    ],
)
def test_worker_delivers_workflow_activity_outcomes(
    fails: bool, legacy_direct_engine: bool
) -> None:
    engine = build_engine()
    started = engine.start("adapter-test", {"subject": "risk-1"})
    run = WorkflowRun.objects.get(pk=started.run_id)
    activity = ActivityRun.objects.get(workflow_run=run)
    # Local failure-mode setup: the first failed attempt must be terminal,
    # so this test exercises failure delivery rather than retry scheduling.
    activity.retry_policy = {"max_attempts": 1}
    activity.save(update_fields=["retry_policy", "updated_at"])

    def execute(context: ActivityContext, input: JsonObject) -> JsonValue:
        if fails:
            raise ExpectedActivityError("expected failure")
        return {"subject": input["subject"], "done": True}

    activities = ActivityRegistry()
    activities.register("tests.work", execute)
    # Compatibility is explicit and local: old WorkerRuntime keeps its engine
    # and advances inline; DjangoRuntime uses the durable inbox by default.
    runtime = (
        WorkerRuntime(activities, engine) if legacy_direct_engine else DjangoRuntime(activities)
    )
    worker = AceWorker(runtime, worker_id="worker-test", now=lambda: FROZEN_NOW)

    assert worker.run_once() is True

    activity.refresh_from_db()
    run.refresh_from_db()
    assert activity.status == (ActivityStatus.FAILED if fails else ActivityStatus.SUCCEEDED)
    assert activity.current_attempt == 1
    assert activity.attempts.get().status == (
        AttemptStatus.FAILED if fails else AttemptStatus.SUCCEEDED
    )
    assert activity.completed_at == FROZEN_NOW
    payload: JsonObject = {
        "activity_key": activity.activity_key,
        "activity_run_id": str(activity.id),
    }
    if fails:
        failure: JsonObject = {
            "error_type": "ExpectedActivityError",
            "message": "expected failure",
            "details": {},
        }
        assert activity.failure == failure
        payload["failure"] = failure
    else:
        result: JsonObject = {"subject": "risk-1", "done": True}
        assert activity.result == result
        payload["result"] = result

    if legacy_direct_engine:
        assert not WorkflowInboxEvent.objects.filter(workflow_run=run).exists()
        assert run.last_event_sequence == started.last_event_sequence + 1
        assert run.state == {"phase": "failed" if fails else "timer"}
        assert run.status == (WorkflowStatus.FAILED if fails else WorkflowStatus.WAITING)
    else:
        inbox = WorkflowInboxEvent.objects.get(workflow_run=run)
        assert inbox.status == InboxStatus.PENDING
        assert inbox.source_type == "activity"
        assert inbox.source_key == activity.activity_key
        assert inbox.event_type == (
            WorkflowEventType.ACTIVITY_FAILED if fails else WorkflowEventType.ACTIVITY_COMPLETED
        )
        assert inbox.payload == payload
        assert inbox.occurred_at == FROZEN_NOW
        assert inbox.processed_at is None
        assert run.last_event_sequence == started.last_event_sequence
        assert run.state == started.state
        assert run.status == started.status
        assert run.result is None
        assert run.failure is None
        assert not run.timers.exists()


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


@pytest.mark.parametrize("runtime_type", [DjangoRuntime, WorkerRuntime])
def test_runtime_factory_preserves_supported_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime_type: type[DjangoRuntime] | type[WorkerRuntime],
) -> None:
    runtime = runtime_type(ActivityRegistry())

    def import_factory(path: str):
        assert path == "tests.runtime_factory"
        return lambda: runtime

    monkeypatch.setattr("ace_django.worker.import_string", import_factory)
    with override_settings(ACE_RUNTIME_FACTORY="tests.runtime_factory"):
        loaded = load_runtime()

    assert loaded is runtime
    assert isinstance(loaded, runtime_type)
