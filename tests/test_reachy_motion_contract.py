"""Real host/edge wire and runtime dispatch, fake localhost motors only."""

import asyncio
import json
import math
from contextlib import asynccontextmanager
from threading import Event as ThreadEvent
from types import SimpleNamespace
from uuid import uuid4

import pytest
from asgiref.sync import sync_to_async
from django.db import connections
from rest_framework.exceptions import PermissionDenied

from agent_reachy import StudioClient
from agent_reachy.daemon_motion import ANTENNAS, DOA, GOTO, POSE, RUNNING, STOP, DaemonMotion
from agent_reachy.errors import RequestTimeout
from agent_reachy.motion import MotionLane
from agent_reachy.motion_diagnostics import Event, Reason
from agent_reachy.studio_client import ACTIONS
from agent_reachy.transport import HTTPResponse
from django_agent_runtime.dynamic_tools.executor import DynamicToolExecutor
from django_agent_runtime.models import LiveVoiceDelegation
from django_agent_runtime.runtime.dynamic import DynamicAgentRuntime
from django_agent_runtime.runtime.tool_authorization import authorize_tool_call
from reachy import actions, controls
from tests_reachy.test_actions import (  # noqa: F401 - explicit isolated host fixtures
    ARGS, NAME, action_host, binding, boundary, command, configured, controls_host, enabled,
    host_opt_in, invoke, live_host, paired, payload, registered, running, start, world,
)

# The real async runtime uses multiple DB connections. Commit fixtures instead
# of holding pytest's outer SQLite transaction; all data stays in the test DB.
pytestmark = pytest.mark.django_db(transaction=True)
WAIT_SECONDS = 5


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, WAIT_SECONDS)


class DeviceHTTP:
    """Real endpoints, serialized with runtime ORM work rather than racing SQLite."""

    credential = SimpleNamespace(device=True)

    def __init__(self, api, loop=None):
        self.api, self.loop = api, loop
        self.exchanges = []
        self.pending = []
        self.fail_next_get = None
        self.failures = 0

    def request(self, method, path, body, *, timeout):
        if self.loop is None:
            return self._send(method, path, body)
        # MotionLane still calls StudioClient in its real daemon_call thread.
        # Only Django I/O is marshalled back onto the SAME thread-sensitive
        # executor as authorize/enqueue/read_result; parsing/observe are real.
        future = asyncio.run_coroutine_threadsafe(
            sync_to_async(self._send, thread_sensitive=True)(method, path, body), self.loop,
        )
        self.pending.append(future)
        return future.result(timeout=timeout)

    def _send(self, method, path, body):
        assert path == ACTIONS  # Never retain credential-bearing endpoint data.
        response = self.api.generic(method, path, body or b"", content_type="application/json")
        self.exchanges.append(SimpleNamespace(
            method=method, sent=json.loads(body) if body else None,
            status=response.status_code, received=response.json(),
        ))
        if method == "GET" and self.fail_next_get:
            # Named lost-response scenario: execute the real endpoint first.
            failure = self.fail_next_get
            self.fail_next_get = None
            self.failures += 1
            if failure == "timeout":
                raise RequestTimeout()
            raise OSError("Simulated lost action response")
        return HTTPResponse(response.status_code, response.content)

    @property
    def acknowledgements(self):
        return [row.sent for row in self.exchanges if row.method == "POST"]


class LocalMotors:
    """Only emulate the documented daemon endpoints; never open a socket."""

    def __init__(self, *, hold_first=False, fail_goto=False):
        self.loop = asyncio.get_running_loop()
        self.pose = dict(x=0., y=0., z=0., roll=0., pitch=0., yaw=0.)
        self.ears = [0., 0.]
        self.moves = []
        self.move_ids = []
        self.stops = []
        self.hold_first, self.fail_goto = hold_first, fail_goto
        self.first_move = asyncio.Event()
        self.stopped = asyncio.Event()
        self.release = ThreadEvent()

    def __call__(self, method, path, payload=None, *, timeout):
        if method == "GET":
            return {POSE: dict(self.pose), ANTENNAS: list(self.ears), RUNNING: [],
                    DOA: None}[path]
        assert method == "POST"
        if path == GOTO:
            self.moves.append(payload)
            self.pose, self.ears = payload["head_pose"], payload["antennas"]
            self.move_ids.append(str(uuid4()))
            self.loop.call_soon_threadsafe(self.first_move.set)
            # Named late-native-reply scenario. Hold only this fake REST call,
            # not the event loop/DB executor, until authority loss is observed.
            if self.hold_first and len(self.moves) == 1:
                if not self.release.wait(WAIT_SECONDS):
                    raise TimeoutError("First fake segment was not released")
            if self.fail_goto:
                raise OSError("Simulated unknown goto outcome")
            return {"uuid": self.move_ids[-1]}
        assert path == STOP
        self.stops.append(payload["uuid"])
        self.loop.call_soon_threadsafe(self.stopped.set)
        return {}


class LaneEvents:
    """Observe existing lane diagnostics; never replace its state machine."""

    def __init__(self):
        self.events = {event: asyncio.Event() for event in Event}

    def emit(self, event, **fields):
        self.events[event].set()

    async def wait(self, event):
        await bounded(self.events[event].wait())


@asynccontextmanager
async def lane_case(paired, **motor_options):
    # asyncio.run (not async_to_sync) gives all host work one shared
    # thread-sensitive executor, including calls arriving from daemon threads.
    loop = asyncio.get_running_loop()
    transport = DeviceHTTP(paired.client, loop)
    client = StudioClient(None, transport)
    motors = LocalMotors(**motor_options)
    executor = DaemonMotion(request=motors)
    attempt = SimpleNamespace(revision=1, deadline=loop.time() + 10,
                              ready=asyncio.Event(), stopped=asyncio.Event(), media=None)
    events = LaneEvents()
    lane = MotionLane(client, executor, attempt, diagnostics=events)
    case = SimpleNamespace(transport=transport, client=client, motors=motors, executor=executor,
                           attempt=attempt, events=events, lane=lane,
                           task=asyncio.create_task(lane.run()), dispatch=None)
    try:
        yield case
    finally:
        lane.stop_local()  # Fence before cancellation or releasing a late reply.
        motors.release.set()
        tasks = [task for task in (case.task, case.dispatch) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await bounded(asyncio.gather(*tasks, return_exceptions=True))
        await bounded(asyncio.gather(
            *(asyncio.wrap_future(future) for future in transport.pending), return_exceptions=True,
        ))

        def drain():
            # Cancellation does not join daemon_call's native worker. Wait for
            # its serialized lane before checking counters/tearing down the DB.
            assert executor._lane.acquire(timeout=WAIT_SECONDS)
            executor._lane.release()
            connections.close_all()

        await bounded(sync_to_async(drain, thread_sensitive=True)())


def runtime_dispatch(world, running, *, name=NAME, arguments=ARGS):
    runtime = DynamicAgentRuntime(world.agent)
    tool = world.agent.tools.get(name=name)
    tool_map = runtime._build_tool_map({"tools": [tool.to_schema()]})

    async def dispatch():
        assert await authorize_tool_call(
            tool_name=name, tool_args=arguments, tool_access="manual", exposed_tool_names=frozenset({name}),
            ctx=running.ctx, agent_key=world.agent.slug,
        ) is None
        return await runtime._execute_tool(
            name, arguments, tool_map, DynamicToolExecutor(), running.ctx,
        )

    return dispatch


@pytest.mark.parametrize("delegated", [False, True], ids=["text", "live"])
@pytest.mark.parametrize("animation", ["nod", "shake_head", "antenna_wiggle"])
def test_runtime_to_ready_lane_waiting_for_first_motion_grant(
    binding, paired, running, configured, boundary, world, delegated, animation,
):
    _, session = start(world, binding, paired)
    if delegated:
        LiveVoiceDelegation.objects.create(
            session=session, run=running.run, state="running", provider_id="offline-motion",
            offset_ms=0, input_revision=session.input_revision,
        )
    arguments = {"animation": animation}
    dispatch = runtime_dispatch(world, running, arguments=arguments)

    async def scenario():
        async with lane_case(paired) as case:
            await case.events.wait(Event.WAIT_READY)
            assert not case.task.done() and case.lane.polls == 0
            assert case.transport.exchanges == [] and case.motors.moves == []
            case.transport.fail_next_get = "timeout"
            case.attempt.ready.set()
            await case.events.wait(Event.WAIT_GRANT)
            assert case.transport.failures == 1 and case.lane.poll_recovery_used is True
            disabled = case.transport.exchanges[0].received
            assert disabled["enabled"] is False and disabled["command"] is None
            assert disabled["voice_revision"] == case.attempt.revision == 1
            assert disabled["grant_revision"] == case.lane.highest_grant == 0
            assert case.lane.observed_voice == 1 and case.lane.grant is None
            assert not case.task.done() and case.motors.moves == []

            await sync_to_async(actions.set_motion, thread_sensitive=True)(
                user=world.editor, thread=binding.thread, value=True, revision=0,
            )
            await case.events.wait(Event.GRANT)
            case.dispatch = asyncio.create_task(dispatch())
            result = await bounded(case.dispatch)
            await case.events.wait(Event.ACKED)
            expected = {"completed": True, "code": "completed", "animation": animation}
            assert result == {"success": True, "status": "succeeded", "result": expected}
            assert not case.task.done() and not case.executor.stopped
            assert len(case.motors.moves) == 3
            if animation == "nod":
                values = [math.degrees(move["head_pose"]["pitch"]) for move in case.motors.moves]
                assert values == pytest.approx([3, -3, 0])
            elif animation == "shake_head":
                values = [math.degrees(move["head_pose"]["yaw"]) for move in case.motors.moves]
                assert values == pytest.approx([3, -3, 0])
            else:
                values = [math.degrees(value) for move in case.motors.moves
                          for value in move["antennas"]]
                assert values == pytest.approx([3, -3, -3, 3, 0, 0])
            assert [ack["state"] for ack in case.transport.acknowledgements] == ["claimed", "succeeded"]
            saved = await sync_to_async(command, thread_sensitive=True)(binding)
            assert saved["state"] == "succeeded" and saved["consumed"] is True
            assert saved["result"] == expected
            assert saved["run_id"] == str(running.run.pk) and saved["arguments"] == arguments
            assert saved["created_at"] < saved["execution_deadline"] <= saved["expires_at"]
            assert saved["voice_revision"] == saved["grant_revision"] == case.lane.grant == 1
            assert {ack["command_id"] for ack in case.transport.acknowledgements} == {saved["id"]}
            assert case.lane.seen == {saved["id"]}
            assert all(row.status == 200 for row in case.transport.exchanges)
            # GETs after the claim must not replay even still-running work.
            claim_index = next(i for i, row in enumerate(case.transport.exchanges) if row.method == "POST")
            after_claim = case.transport.exchanges[claim_index + 1:]
            assert all(row.received["command"] is None for row in after_claim)
            return saved

    saved = asyncio.run(scenario())
    assert StudioClient(None, DeviceHTTP(paired.client)).actions().command is None
    with pytest.raises(PermissionDenied):
        actions.read_result(running.execution, {
            "conversation_id": str(binding.conversation.pk), "command_id": saved["id"],
        })


@pytest.mark.parametrize("loss", ["motion-off", "lost-host-response"])
def test_authority_loss_during_first_segment_fences_lane_without_retry(
    world, binding, paired, running, enabled, loss,
):
    dispatch = runtime_dispatch(world, running)

    async def scenario():
        async with lane_case(paired, hold_first=True) as case:
            case.attempt.ready.set()
            await case.events.wait(Event.GRANT)
            case.dispatch = asyncio.create_task(dispatch())
            await bounded(case.motors.first_move.wait())
            assert case.lane.execution is not None and not case.lane.execution.done()
            if loss == "motion-off":
                await sync_to_async(actions.set_motion, thread_sensitive=True)(
                    user=world.editor, thread=binding.thread, value=False, revision=1,
                )
            else:
                case.transport.fail_next_get = "transport"
            await bounded(case.task)
            # OFF rotates the revision, so observe rejects that before enabled.
            reason = Reason.GRANT_REVISION if loss == "motion-off" else Reason.REQUEST_FAILED
            assert case.lane.reason is reason
            assert case.executor.stopped and case.lane.execution.cancelled()
            if loss == "motion-off":
                disabled = case.transport.exchanges[-1].received
                assert disabled["enabled"] is False and disabled["command"] is None
                assert disabled["voice_revision"] == 1 and disabled["grant_revision"] == 2
            else:
                assert case.transport.failures == 1 and not case.transport.fail_next_get
            requests_at_fence = len(case.transport.exchanges)
            case.motors.release.set()
            await bounded(case.motors.stopped.wait())
            result = await bounded(case.dispatch)
            assert result["success"] is False
            assert len(case.transport.exchanges) == requests_at_fence  # No retry/poll/ack after fencing.
            assert [ack["state"] for ack in case.transport.acknowledgements] == ["claimed"]
        # The native lane is drained, not merely its cancelled asyncio wrapper.
        assert len(case.motors.moves) == 1
        assert case.motors.stops == case.motors.move_ids

    asyncio.run(scenario())
    saved = command(binding)
    assert saved["state"] == "failed" and saved["consumed"] is True


def test_unknown_daemon_request_outcome_is_not_retried(binding, paired, running, enabled, world):
    dispatch = runtime_dispatch(world, running)

    async def scenario():
        async with lane_case(paired, fail_goto=True) as case:
            case.attempt.ready.set()
            await case.events.wait(Event.GRANT)
            case.dispatch = asyncio.create_task(dispatch())
            await bounded(case.task)
            result = await bounded(case.dispatch)
            assert result == {"success": False, "status": "failed",
                              "result": {"completed": False, "code": "unavailable"}}
            assert case.lane.reason is Reason.EXECUTION_FAILED and case.executor.stopped
            assert [ack["state"] for ack in case.transport.acknowledgements] == ["claimed", "failed"]
        assert len(case.motors.moves) == 1 and case.motors.stops == []  # No known UUID to stop.

    asyncio.run(scenario())
    saved = command(binding)
    assert saved["state"] == "failed" and saved["consumed"] is True


@pytest.mark.parametrize("previous_grant", [False, True], ids=["initial-grant-zero", "stale-grant-voice"])
def test_disabled_wire_uses_current_voice_revision(
    world, binding, paired, running, configured, boundary, previous_grant,
):
    start(world, binding, paired)
    if previous_grant:
        actions.set_motion(user=world.editor, thread=binding.thread, value=True, revision=0)
        assert invoke(world, binding, False, 1).status_code == 200
        assert invoke(world, binding, True, 2).status_code == 200
    binding.conversation.refresh_from_db()
    grant = actions.action_state(binding.conversation)["grants"]["motion"]
    current = controls.voice_state(binding.conversation)["revision"]
    assert grant["voice_revision"] == (1 if previous_grant else 0)
    assert current == (3 if previous_grant else 1)
    assert grant["voice_revision"] != current and grant["enabled"] is False
    transport = DeviceHTTP(paired.client)
    state = StudioClient(None, transport).actions()
    wire = transport.exchanges[-1].received
    assert wire["voice_revision"] == state.voice_revision == current
    assert wire["grant_revision"] == state.grant_revision == (2 if previous_grant else 0)
    assert state.enabled is False and state.remaining_seconds == 0 and state.command is None
    assert wire["capabilities"] == {name: False for name in actions.CAPABILITIES}