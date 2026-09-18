"""CLI adapter -> HTTPConnection bridge -> authenticated runtime REST views."""

from secrets import token_hex

import pytest
from agentctl.errors import CLIError
from django_agent_runtime.models import AgentRun

from .fixtures import Actor, create_agent

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("group,resource,status", [
    ("runtime", "agents", 401),
    ("runtime", "runs", 401),
    # Studio retains SessionAuthentication first, so DRF returns 403, not 401.
    ("studio", "projects", 403),
    ("studio", "threads", 403),
])
@pytest.mark.parametrize("rejection", ["invalid", "revoked", "inactive"])
def test_real_token_authentication_rejects_unusable_credentials(
    world, api_for, group, resource, status, rejection,
):
    actor = world.owner
    api = api_for(actor)
    assert isinstance(api.list(group, resource)["items"], list)
    if rejection == "invalid":
        # A random unissued credential, kept out of parametrization/test IDs.
        api = api_for(Actor(actor.user, token_hex(20)))
    elif rejection == "revoked":
        actor.revoke_token()
    else:
        actor.deactivate()
    with pytest.raises(CLIError) as rejected:
        api.list(group, resource)
    assert rejected.value.status == status


def test_runtime_agent_list_and_slug_detail_use_existing_serializer(world, api_for, client_for):
    api = api_for(world.owner)
    page = api.list("runtime", "agents")
    assert page["count"] == 1
    assert page["pages"] == 1
    assert page["has_more"] is False
    assert page["items"][0]["slug"] == world.agent["slug"]
    detail = api.get("runtime", "agents", world.agent["slug"])
    assert detail == page["items"][0]
    assert set(detail) == {"id", "slug", "name", "is_active", "active_version"}
    raw = client_for(world.owner).get(f"/api/agent-runtime/agents/{world.agent['slug']}/")
    assert {key: raw[key] for key in detail} == detail
    assert set(raw) == {
        "id", "slug", "name", "description", "icon", "is_active", "active_version", "versions",
    }
    # Runtime discovery has its existing host policy, NOT Studio's ownership
    # filter. Do not invent a stricter production policy in a test-host subclass.
    assert api_for(world.outsider).get("runtime", "agents", world.agent["slug"]) == detail


def test_queued_run_list_is_owner_scoped_filtered_and_never_dispatches(
    world, api_for, queued_run,
):
    own = queued_run(world.owner, world.agent, thread=world.thread)
    other_agent = create_agent(world.outsider, "contract-other-agent")
    other = queued_run(world.outsider, other_agent)
    before = list(AgentRun.objects.order_by("pk").values())
    for actor, expected in ((world.owner, own), (world.outsider, other)):
        page = api_for(actor).list("runtime", "runs")
        assert page["count"] == 1
        assert [row["id"] for row in page["items"]] == [str(expected.pk)]
        assert page["items"][0]["status"] == "queued"
        assert page["has_more"] is False
        assert not {"input", "output", "error", "metadata"} & page["items"][0].keys()
    assert api_for(world.owner).list(
        "runtime", "runs", query={"agent_key": other_agent["slug"]},
    )["items"] == []
    assert api_for(world.guest).list("runtime", "runs")["items"] == []
    assert list(AgentRun.objects.order_by("pk").values()) == before


def test_run_detail_is_refused_before_transport_even_with_a_real_mounted_route(
    world, api_for, queued_run, bridge,
):
    run = queued_run(world.owner, world.agent, thread=world.thread)
    api = api_for(world.owner)
    before = len(bridge.requests)
    with pytest.raises(CLIError):
        api.get("runtime", "runs", str(run.pk))
    assert len(bridge.requests) == before
    run.refresh_from_db()
    assert run.status == "queued"
    assert run.attempt == AgentRun._meta.get_field("attempt").get_default()


def test_optional_host_identity_uses_real_token_and_rechecks_revocation(world, api_for):
    api = api_for(world.owner, identity_path="/host/identity/")
    assert api.whoami() == {
        "id": str(world.owner.user.pk), "username": world.owner.user.get_username(),
    }
    world.owner.revoke_token()
    with pytest.raises(CLIError) as rejected:
        api.whoami()
    assert rejected.value.status == 401
