"""Cross-repository contract: real host HTTP shapes, fake provider, isolated DB.

Run with PYTHONPATH=packages/python:products/studio:clients/agent-reachy/src and
--ds=django_agent_studio.tests.settings. No running server or host settings import.
"""

import ast
from types import SimpleNamespace

import pytest
from django.urls import include, path
from django.utils.module_loading import import_string

from agent_reachy import Binding, StudioClient
from agent_reachy.errors import Cancelled, HTTPStatusError
from agent_reachy.transport import HTTPResponse
from django_agent_runtime.api.views import BaseAgentRunViewSet
from django_agent_runtime.models import AgentConversation, AgentDefinition, AgentRun, LiveVoiceSession
from django_agent_runtime.runtime.history import append_turn, history_write_lock
from django_agent_runtime.voice import live
from reachy.controls import VOICE_KEY
from tests_ai_connections.test_settings import assignment, source_tree
from tests_reachy.test_host import (  # noqa: F401 - inherited supported defaults/writers
    OFFER, binding, boundary, configured, device_client, host_opt_in, live_host, payload, request, world,
)


pytestmark = pytest.mark.django_db
urlpatterns = [
    path("api/", include("django_agent_studio.api.urls")),
    path("api/agent-runtime/", include("django_agent_runtime.urls")),
    path("api/reachy/", include("reachy.urls")),
]


class InProcessHTTP:
    def __init__(self, client):
        self.client = client
        self.shapes = []

    def request(self, method, path, body, *, timeout):
        response = self.client.generic(method, path, body or b"", content_type="application/json")
        self.shapes.append(set(response.json()))  # Names only; no payload/secret diagnostics.
        return HTTPResponse(response.status_code, response.content)


def test_client_against_host_renderers_uses_one_agent_and_conversation(
        settings, monkeypatch, world, binding, configured, boundary, live_host):
    # Reproduce the actual host's API transport defaults without importing .env,
    # cloud settings or a host database. No storage, queue or access overrides.
    rest = ast.literal_eval(assignment(source_tree("base.py"), "REST_FRAMEWORK"))
    for attribute, setting in (("authentication_classes", "DEFAULT_AUTHENTICATION_CLASSES"),
                               ("permission_classes", "DEFAULT_PERMISSION_CLASSES"),
                               ("renderer_classes", "DEFAULT_RENDERER_CLASSES"),
                               ("parser_classes", "DEFAULT_PARSER_CLASSES")):
        monkeypatch.setattr(BaseAgentRunViewSet, attribute, [import_string(p) for p in rest[setting]])
    settings.ROOT_URLCONF = __name__
    transport = InProcessHTTP(binding.client)
    client = StudioClient(Binding(world.agent.slug, str(binding.conversation.pk)), transport)
    text = client.send_text("Hello through the ordinary runtime")
    assert {"agentKey", "conversationId"} <= transport.shapes[-1]
    run = AgentRun.objects.get(pk=text.id)
    assert run.agent_key == world.agent.slug and run.conversation_id == binding.conversation.pk
    assert run.status == "queued" and not run.metadata.get("_live_voice")
    assert run.input["params"]["studio_ai_settings"]["model"] == configured.model
    # Synthetic completion uses the canonical writer under its transaction lock;
    # this tests stored/read API contracts, not a model's inference quality.
    with history_write_lock(binding.conversation):
        run.status = "succeeded"
        run.output = {"final_output": {"response": "Text reply"},
                      "final_messages": [{"role": "assistant", "content": "Text reply"}]}
        run.save(update_fields=["status", "output"])
        assert append_turn(run.pk, expected_attempt=run.attempt) == 2
    assert client.read_run(text.id).assistant_text() == "Text reply"
    device_api, _token, _device_id = device_client(binding)
    # The scoped device scheme is not accepted by the ordinary runtime routes.
    assert device_api.post("/api/agent-runtime/runs/", {}, format="json").status_code == 401
    device_transport = InProcessHTTP(device_api)
    voice = StudioClient(None, device_transport).live_session()
    answer = voice.start(OFFER)
    assert device_transport.shapes[0] == {"agent_key", "conversation_id", "device_id"}
    assert "conversation_id" in device_transport.shapes[-1]
    assert "conversationId" not in device_transport.shapes[-1]
    session = LiveVoiceSession.objects.get(pk=answer.id)
    assert session.agent_key == text.binding.agent_key
    assert str(session.conversation_id) == text.binding.conversation_id
    assert voice.status().state == "ready"
    assert voice.close().state == "ready"  # Accepted, not proof of provider close.
    assert voice.close_accepted
    assert AgentDefinition.objects.count() == AgentConversation.objects.count() == 1
    assert binding.conversation.messages.count() == 2


def test_browser_controls_drive_edge_voice_and_off_preserves_cleanup(
        settings, world, binding, configured, boundary, live_host):
    # Named host controls opt-in, local to this lifecycle contract. Keep storage,
    # ACLs, queues and bounds unchanged; inherited fixtures fake provider I/O
    # and block sockets. The ordinary text/live contract above stays unmanaged.
    settings.AGENT_STUDIO_CONTROLS_BACKEND = "reachy.controls.ReachyControlsBackend"
    settings.ROOT_URLCONF = __name__
    route = f"threads/{binding.thread.pk}/controls/"

    def browser_control(method="get", data=None):
        response = request(world.editor, method, route, data)
        assert response.status_code == 200
        projection = response.json()
        assert set(projection) == {"schema_version", "controls"}
        assert projection["schema_version"] == 1
        assert [item["id"] for item in projection["controls"]] == ["reachy.voice", "reachy.motion"]
        assert projection["controls"][1]["value"] is False
        row = projection["controls"][0]
        assert row["id"] == "reachy.voice" and row["type"] == "toggle"
        assert row["disabled"] is False
        return row

    discovery = request(world.editor, "get", route)
    assert discovery.status_code == 200
    assert discovery.json() == {"schema_version": 1, "controls": []}
    transport = InProcessHTTP(device_client(binding)[0])
    # The APIClient already carries real device authentication. Only advertise
    # its credential kind to the edge client; never copy a token into this shim.
    transport.credential = SimpleNamespace(device=True)
    client = StudioClient(None, transport)
    row = browser_control()
    assert row["value"] is False and row["revision"] == 0
    assert row["status"] == "Off requested; device not confirmed"
    command = client.voice_command()
    assert command.revision == 0 and command.enabled is False
    assert command.remaining_seconds == 0 and command.claimed is False
    binding.conversation.refresh_from_db()
    assert VOICE_KEY not in set(binding.conversation.metadata)

    assert client.ack_voice(command.revision, "idle") == command
    assert browser_control()["status"] == "Stopped"
    row = browser_control("post", {"control_id": "reachy.voice", "value": True, "revision": 0})
    assert row["revision"] == 1 and row["value"] is True
    assert row["status"] != "Listening"  # Intent is not an observation.
    command = client.voice_command()
    assert command.revision == row["revision"] and command.enabled is True
    assert 0 < command.remaining_seconds <= 300 and command.claimed is False
    starting = client.ack_voice(command.revision, "starting")
    assert starting.revision == command.revision and starting.enabled is True
    assert starting.claimed is False
    assert browser_control()["status"] == "Starting"
    assert not LiveVoiceSession.objects.exists()
    assert boundary.provider.create_webrtc.call_count == 0

    session = client.live_session(control_revision=command.revision)
    session.start(OFFER)  # Exercise real binding discovery and SDP parsing, not diagnostics.
    stored = LiveVoiceSession.objects.get(pk=session.id)
    assert session.binding.agent_key == stored.agent_key == world.agent.slug
    assert session.binding.conversation_id == str(stored.conversation_id) == str(binding.conversation.pk)
    assert stored.user_id == world.editor.pk and stored.state == "ready"
    assert session.status().state == "ready" and session.stopped is False
    listening = client.ack_voice(command.revision, "listening")
    assert listening.revision == command.revision and listening.enabled is True
    assert listening.claimed is True
    row = browser_control()
    assert row["value"] is True and row["revision"] == command.revision
    assert row["status"] == "Listening"

    # Neither this handle nor a fresh handle may replay the claimed revision.
    requests = len(transport.shapes)
    with pytest.raises(Cancelled):
        session.start(OFFER)
    assert len(transport.shapes) == requests
    duplicate = client.live_session(control_revision=command.revision)
    with pytest.raises(HTTPStatusError) as rejected:
        duplicate.start(OFFER)
    assert rejected.value.status == 409 and duplicate.stopped is True
    assert LiveVoiceSession.objects.count() == 1
    assert boundary.provider.create_webrtc.call_count == 1

    row = browser_control("post", {
        "control_id": "reachy.voice", "value": False, "revision": command.revision,
    })
    assert row["revision"] == command.revision + 1 and row["value"] is False
    assert row["status"] == "Off requested; device not confirmed"
    off = client.voice_command()
    assert off.revision == row["revision"] and off.enabled is False
    assert off.remaining_seconds == 0 and off.claimed is False
    stored.refresh_from_db()
    assert stored.close_requested_at is not None
    assert session.status().state == "closing"
    assert session.stopped is True

    binding.conversation.refresh_from_db()
    before = dict(binding.conversation.metadata[VOICE_KEY])
    before_session = (stored.state, stored.close_requested_at)
    with pytest.raises(HTTPStatusError) as stale:
        client.ack_voice(command.revision, "listening")
    assert stale.value.status == 409
    binding.conversation.refresh_from_db()
    stored.refresh_from_db()
    # Compare only control state and safe session fields, never device verifiers
    # or provider responses in assertion diagnostics.
    assert binding.conversation.metadata[VOICE_KEY] == before
    assert (stored.state, stored.close_requested_at) == before_session
    assert client.voice_command() == off

    # OFF fences media, not owner identity needed to finalize retained history.
    assert live.owner_valid(stored) is True
    requests = len(transport.shapes)
    closed = session.close_once()
    assert closed.id == session.id and session.close_accepted is True
    assert session.close_once() == closed
    assert len(transport.shapes) == requests + 1
    assert client.ack_voice(off.revision, "stopped") == off
    row = browser_control()
    assert row["status"] == "Stopped" and row["value"] is False
    assert row["revision"] == off.revision
    assert LiveVoiceSession.objects.count() == 1
    assert boundary.provider.create_webrtc.call_count == 1
    assert AgentDefinition.objects.count() == AgentConversation.objects.count() == 1
    assert not AgentRun.objects.exists() and not binding.conversation.messages.exists()