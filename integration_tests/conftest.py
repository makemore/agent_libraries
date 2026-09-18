"""All setup writes precede reads guarded by the in-process REST bridge."""

import http.client
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings

# Fail during collection, before any test database fixture can run.
if settings.SETTINGS_MODULE != "integration_tests.settings":
    raise pytest.UsageError("Use integration_tests.settings, never host settings")

from django_agent_runtime.api.views import BaseAgentRunViewSet
from django_agent_runtime.models import AgentRun

from .bridge import DjangoBridge
from .fixtures import (
    Actor,
    create_agent,
    create_organization,
    create_project,
    create_thread,
    project_member,
)


@pytest.fixture(autouse=True)
def isolated_host_and_no_dispatch(monkeypatch):
    if settings.SETTINGS_MODULE != "integration_tests.settings":
        pytest.fail("Use integration_tests.settings, never host settings")

    def forbidden(*args, **kwargs):
        raise AssertionError("Runtime dispatch must never be invoked by REST reads")

    monkeypatch.setattr(BaseAgentRunViewSet, "_schedule_dispatch", forbidden)
    monkeypatch.setattr(BaseAgentRunViewSet, "_dispatch_accepted_run", forbidden)


@pytest.fixture
def bridge(monkeypatch):
    transport = DjangoBridge()

    def request(connection, method, url, body=None, headers=None, *, encode_chunked=False):
        __tracebackhide__ = True
        connection._contract_response = transport.request(connection, method, url, body, headers)

    # Keep the real Client, URL/query/header construction, error handling and
    # decoder. Only replace HTTPConnection's socket send/receive boundary.
    monkeypatch.setattr(http.client.HTTPConnection, "request", request)
    monkeypatch.setattr(http.client.HTTPConnection, "getresponse", lambda conn: conn._contract_response)
    return transport


@pytest.fixture
def client_for(bridge):
    from agentctl.transport import Client

    def make(actor):
        __tracebackhide__ = True
        return Client("http://127.0.0.1", actor._token, allow_http_loopback=True)

    return make


@pytest.fixture
def api_for(client_for):
    from agentctl.resources import ReadAPI

    def make(actor, **mounts):
        paths = {"runtime_mount": "/api/agent-runtime/", "studio_mount": "/studio/api/"}
        return ReadAPI(client_for(actor), **{**paths, **mounts})

    return make


@pytest.fixture
def world(db):
    owner, admin, member, guest, outsider = [Actor.create(name) for name in (
        "contract-owner", "contract-admin", "contract-member", "contract-guest", "contract-outsider",
    )]
    organization = create_organization(owner, "First organization")
    for actor, role in ((admin, "admin"), (member, "member")):
        owner.workspace("post", f"organizations/{organization['id']}/members/", {
            "username": actor.user.get_username(), "role": role,
        }, status=201)
    project = create_project(owner, organization, "First project")
    hidden = create_project(owner, organization, "Hidden sibling")
    project_member(owner, project, guest)
    other_org = create_organization(outsider, "Other organization")
    other_project = create_project(outsider, other_org, "Other project")
    agent = create_agent(owner, "contract-agent")
    thread = create_thread(owner, project, agent)
    return SimpleNamespace(
        owner=owner, admin=admin, member=member, guest=guest, outsider=outsider,
        organization=organization, project=project, hidden=hidden,
        other_org=other_org, other_project=other_project, agent=agent, thread=thread,
    )


@pytest.fixture
def queued_run():
    def make(actor, agent, *, thread=None):
        payload = {"agent_key": agent["slug"], "messages": []}
        if thread is not None:
            payload["conversation_id"] = thread["conversation_id"]
        # Named queued/no-worker scenario: only dispatch scheduling is mocked,
        # locally to creation. REST validation, history initialization and the
        # durable queued row are real. No fake completed turns or provider work.
        with patch.object(BaseAgentRunViewSet, "_schedule_dispatch") as dispatch:
            row = actor.request("post", "/api/agent-runtime/runs/", payload, status=201)
            dispatch.assert_called_once()
        run = AgentRun.objects.get(pk=row["id"])
        assert run.status == "queued"
        assert run.attempt == AgentRun._meta.get_field("attempt").get_default()
        return run

    return make
