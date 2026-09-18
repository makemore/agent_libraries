"""PostgreSQL regression: a claim must not lock unrelated ready activities."""

from multiprocessing import get_context

import pytest
from ace_django.models import ActivityAttempt, ActivityRun, ActivityStatus
from ace_django.queue import DjangoActivityQueue
from ace_django.tests.helpers import FROZEN_NOW
from ace_django.tests.process_helpers import hold_activity_claim
from django.db import connection, connections

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_uncommitted_claim_leaves_other_ready_rows_claimable(monkeypatch):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL row locks required")
    monkeypatch.setenv("ACE_DB_NAME", str(connection.settings_dict["NAME"]))
    for index in range(2):
        ActivityRun.objects.create(
            activity_key=f"claim-scope-{index}", activity_name="tests.work", input={},
            available_at=FROZEN_NOW,
        )
    connections.close_all()
    context = get_context("spawn")
    output, release = context.Queue(), context.Event()
    child = context.Process(target=hold_activity_claim, args=(output, release))
    child.start()
    try:
        first = output.get(timeout=20)
        assert first is not None
        # The first transaction is still open, so only its one selected activity
        # should be skipped. No sleeps, changed queue limits or retry polling.
        second = DjangoActivityQueue().claim("other-claim", now=FROZEN_NOW)
        assert second is not None
        assert second.activity_run_id != first.activity_run_id
    finally:
        release.set()
        child.join(timeout=20)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
        exitcode = child.exitcode
        if not child.is_alive():
            child.close()
        output.close()
        output.join_thread()
    assert exitcode == 0
    assert ActivityRun.objects.filter(status=ActivityStatus.RUNNING).count() == 2
    assert ActivityAttempt.objects.count() == 2
