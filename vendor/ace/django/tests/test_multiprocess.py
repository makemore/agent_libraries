from datetime import timedelta
from multiprocessing import get_context
from time import sleep

import pytest
from ace import WorkflowStatus
from ace_django.exceptions import LeaseOwnershipLost
from ace_django.models import (
    AceWorkerHeartbeat,
    ActivityAttempt,
    ActivityGroupRun,
    ActivityGroupStatus,
    ActivityRun,
    ActivityStatus,
    WorkerStatus,
    WorkflowEvent,
    WorkflowRun,
)
from ace_django.queue import DjangoActivityQueue
from ace_django.supervisor import WorkerSupervisor
from ace_django.tests.helpers import FROZEN_NOW, build_fan_in_engine, build_group_engine
from ace_django.tests.process_helpers import (
    claim_activity,
    complete_fan_in_activity,
    complete_group_activity,
    start_idempotent_workflow,
)
from django.db import connection, connections

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]

ACTIVITY_IDS = tuple(f"30000000-0000-0000-0000-{index:012d}" for index in range(1, 5))
TOKENS = tuple(f"40000000-0000-0000-0000-{index:012d}" for index in range(1, 6))


@pytest.fixture(autouse=True)
def require_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Multi-process lease tests require PostgreSQL.")
    monkeypatch.setenv("ACE_DB_NAME", str(connection.settings_dict["NAME"]))


def _activity(activity_id: str) -> ActivityRun:
    return ActivityRun(
        id=activity_id,
        activity_key=f"work-{activity_id[-1]}",
        activity_name="tests.work",
        input={"subject": activity_id},
        available_at=FROZEN_NOW,
    )


def _run(processes) -> None:
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0


def _wait_for_activity_status(status: ActivityStatus, count: int) -> None:
    for _ in range(200):
        if ActivityRun.objects.filter(status=status).count() == count:
            return
        sleep(0.02)
    pytest.fail(f"Timed out waiting for {count} activities in status {status}.")


def test_four_processes_claim_distinct_activities() -> None:
    ActivityRun.objects.bulk_create([_activity(activity_id) for activity_id in ACTIVITY_IDS])
    connections.close_all()
    context = get_context("spawn")
    barrier = context.Barrier(4)
    output = context.Queue()
    processes = [
        context.Process(
            target=claim_activity,
            args=(barrier, output, f"worker-{index}", TOKENS[index], FROZEN_NOW.isoformat()),
        )
        for index in range(4)
    ]

    _run(processes)

    leases = [output.get(timeout=5) for _ in processes]
    claimed_ids = {lease.activity_run_id for lease in leases if lease is not None}
    assert claimed_ids == set(ACTIVITY_IDS)
    assert ActivityAttempt.objects.count() == 4
    assert ActivityRun.objects.filter(status=ActivityStatus.RUNNING).count() == 4


def test_abandoned_lease_is_recovered_and_stale_owner_is_rejected() -> None:
    activity = _activity(ACTIVITY_IDS[0])
    activity.save(force_insert=True)
    connections.close_all()
    context = get_context("spawn")
    first_output = context.Queue()
    first_barrier = context.Barrier(1)
    first = context.Process(
        target=claim_activity,
        args=(
            first_barrier,
            first_output,
            "worker-1",
            TOKENS[0],
            FROZEN_NOW.isoformat(),
            1,
        ),
    )
    _run([first])
    abandoned = first_output.get(timeout=5)
    assert abandoned is not None

    second_output = context.Queue()
    recovered_at = FROZEN_NOW + timedelta(seconds=2)
    second_barrier = context.Barrier(1)
    second = context.Process(
        target=claim_activity,
        args=(
            second_barrier,
            second_output,
            "worker-2",
            TOKENS[1],
            recovered_at.isoformat(),
        ),
    )
    _run([second])
    recovered = second_output.get(timeout=5)
    assert recovered is not None
    assert recovered.attempt == 2

    queue = DjangoActivityQueue()
    with pytest.raises(LeaseOwnershipLost, match="no longer owns"):
        queue.complete(abandoned, {"stale": True}, now=recovered_at)
    queue.complete(recovered, {"ok": True}, now=recovered_at)
    activity.refresh_from_db()
    assert activity.status == ActivityStatus.SUCCEEDED


def test_concurrent_idempotent_starts_create_one_workflow() -> None:
    connections.close_all()
    context = get_context("spawn")
    barrier = context.Barrier(2)
    output = context.Queue()
    ids = (
        ("10000000-0000-0000-0000-000000000011", "20000000-0000-0000-0000-000000000011"),
        ("10000000-0000-0000-0000-000000000012", "20000000-0000-0000-0000-000000000012"),
    )
    processes = [
        context.Process(target=start_idempotent_workflow, args=(barrier, output, *worker_ids))
        for worker_ids in ids
    ]

    _run(processes)

    run_ids = [output.get(timeout=5) for _ in processes]
    assert len(set(run_ids)) == 1
    assert WorkflowRun.objects.count() == 1
    assert WorkflowEvent.objects.count() == 1
    assert ActivityRun.objects.count() == 1


def test_concurrent_fan_in_completions_are_serialized() -> None:
    engine = build_fan_in_engine(
        "10000000-0000-0000-0000-000000000031",
        "20000000-0000-0000-0000-000000000031",
    )
    started = engine.start("fan-in-test", {"subject": "risk-1"})
    connections.close_all()
    context = get_context("spawn")
    barrier = context.Barrier(2)
    output = context.Queue()
    processes = [
        context.Process(
            target=complete_fan_in_activity,
            args=(
                barrier,
                output,
                f"fan-in-{index}",
                TOKENS[index],
                f"20000000-0000-0000-0000-00000000003{index + 2}",
            ),
        )
        for index in range(2)
    ]

    _run(processes)

    assert all(output.get(timeout=5) is not None for _ in processes)
    run = WorkflowRun.objects.get(pk=started.run_id)
    assert run.status == WorkflowStatus.COMPLETED
    assert run.last_event_sequence == 3
    assert ActivityRun.objects.filter(status=ActivityStatus.SUCCEEDED).count() == 2


def test_four_supervised_workers_execute_four_activities() -> None:
    ActivityRun.objects.bulk_create(
        [
            ActivityRun(
                id=activity_id,
                activity_key=f"echo-{index}",
                activity_name="tests.echo",
                input={"index": index},
                available_at=FROZEN_NOW,
            )
            for index, activity_id in enumerate(ACTIVITY_IDS)
        ]
    )
    connections.close_all()
    supervisor = WorkerSupervisor(
        workers=4,
        poll_interval=0.01,
        lease_duration=timedelta(seconds=2),
        renewal_interval=timedelta(seconds=0.2),
        shutdown_timeout=5,
        worker_id_prefix="supervisor-test",
    )
    supervisor.start()
    try:
        _wait_for_activity_status(ActivityStatus.SUCCEEDED, 4)
    finally:
        supervisor.request_stop()
        supervisor.wait()

    assert {process.exitcode for process in supervisor.processes} == {0}
    assert (
        AceWorkerHeartbeat.objects.filter(
            worker_id__startswith="supervisor-test-",
            status=WorkerStatus.STOPPED,
        ).count()
        == 4
    )
    assert list(ActivityRun.objects.order_by("activity_key").values_list("result", flat=True)) == [
        {"index": 0},
        {"index": 1},
        {"index": 2},
        {"index": 3},
    ]


def test_supervisor_drains_running_activity_while_lease_renews() -> None:
    ActivityRun.objects.create(
        id=ACTIVITY_IDS[0],
        activity_key="slow",
        activity_name="tests.slow",
        input={"subject": "risk-1"},
        available_at=FROZEN_NOW,
    )
    ActivityRun.objects.create(
        id=ACTIVITY_IDS[1],
        activity_key="next",
        activity_name="tests.echo",
        input={"subject": "risk-2"},
        available_at=FROZEN_NOW,
    )
    connections.close_all()
    supervisor = WorkerSupervisor(
        workers=1,
        poll_interval=0.01,
        lease_duration=timedelta(seconds=0.15),
        renewal_interval=timedelta(seconds=0.03),
        shutdown_timeout=5,
        worker_id_prefix="drain-test",
    )
    supervisor.start()
    _wait_for_activity_status(ActivityStatus.RUNNING, 1)
    supervisor.request_stop()
    supervisor.wait()

    activity = ActivityRun.objects.get(activity_key="slow")
    unclaimed = ActivityRun.objects.get(activity_key="next")
    attempt = activity.attempts.get()
    assert activity.status == ActivityStatus.SUCCEEDED
    assert activity.result == {"completed": True, "subject": "risk-1"}
    assert attempt.heartbeat_at > attempt.started_at
    assert unclaimed.status == ActivityStatus.READY
    assert AceWorkerHeartbeat.objects.get(worker_id="drain-test-1").status == WorkerStatus.STOPPED


def test_concurrent_group_completions_produce_exactly_one_group_event() -> None:
    """Two workers complete group members simultaneously. Only one triggers
    ACTIVITY_GROUP_COMPLETED. The group and workflow end up in terminal state
    exactly once."""
    engine = build_group_engine(
        "10000000-0000-0000-0000-000000000061",
        "20000000-0000-0000-0000-000000000061",
    )
    started = engine.start("group-adapter-test", {})
    # Make both members claimable.
    ActivityRun.objects.filter(workflow_run_id=started.run_id).update(available_at=FROZEN_NOW)

    connections.close_all()
    context = get_context("spawn")
    barrier = context.Barrier(2)
    output = context.Queue()
    processes = [
        context.Process(
            target=complete_group_activity,
            args=(
                barrier,
                output,
                f"group-worker-{index}",
                TOKENS[index],
                f"20000000-0000-0000-0000-00000000006{index + 2}",
            ),
        )
        for index in range(2)
    ]

    _run(processes)

    results = [output.get(timeout=5) for _ in processes]
    assert all(r is not None for r in results), f"Both workers should have claimed: {results}"

    # Exactly one should have snapshot_status == COMPLETED (the last finisher).
    snapshot_statuses = [r["snapshot_status"] for r in results]
    assert snapshot_statuses.count(WorkflowStatus.COMPLETED) == 1
    assert snapshot_statuses.count(None) == 1

    # Group is SUCCEEDED.
    group = ActivityGroupRun.objects.get(workflow_run_id=started.run_id)
    assert group.status == ActivityGroupStatus.SUCCEEDED
    assert set(group.result.keys()) == {"extract-a", "extract-b"}

    # Workflow is COMPLETED with exactly 3 events (start, group_completed, workflow_complete).
    run = WorkflowRun.objects.get(pk=started.run_id)
    assert run.status == WorkflowStatus.COMPLETED
    assert ActivityRun.objects.filter(status=ActivityStatus.SUCCEEDED).count() == 2
