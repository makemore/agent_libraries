"""Supported Studio writes prepare state; every adapter read is write-guarded."""

import pytest
from agentctl.errors import CLIError
from django_agent_runtime import conf
from django_agent_runtime.models import AgentConversation, AgentDefinition
from django_agent_studio.models.workspace import StudioProjectMembership, StudioThread

from .fixtures import WORKSPACE, create_agent, create_thread, project_member

pytestmark = pytest.mark.django_db


def ids(page):
    return {row["id"] for row in page["items"]}


def assert_hidden(api, thread):
    assert thread["id"] not in ids(api.list("studio", "threads"))
    with pytest.raises(CLIError) as denied:
        api.get("studio", "threads", thread["id"])
    assert denied.value.status == 404


def test_supported_thread_creation_inherits_defaults_and_reads_empty_history(
    world, api_for, client_for,
):
    assert AgentDefinition.objects.get(pk=world.agent["id"]).message_storage_mode == ""
    config = conf.runtime_settings()
    assert config.MESSAGE_STORAGE_MODE == "normalized"
    assert config.HISTORY_LAZY_UPGRADE is False
    conversation = AgentConversation.objects.get(pk=world.thread["conversation_id"])
    assert conversation.history_mode == config.MESSAGE_STORAGE_MODE
    assert conversation.history_state == "ready" and conversation.history_version == 1
    assert conversation.user_id == world.owner.user.pk
    assert conversation.title == "" and conversation.metadata == {}
    assert conversation.runs.count() == 0 and conversation.messages.count() == 0
    api = api_for(world.owner)
    detail = api.get("studio", "threads", world.thread["id"])
    assert detail["visibility"] == "private"
    assert not StudioThread.objects.get(pk=world.thread["id"]).shares.exists()
    assert "shared_user_ids" not in detail
    assert detail["can_write"] is True and detail["can_manage"] is True
    assert ids(api.list("studio", "threads")) == {world.thread["id"]}
    history = client_for(world.owner).get(
        WORKSPACE + f"conversations/{conversation.pk}/",
    )
    assert history["history_mode"] == "normalized" and history["history_state"] == "ready"
    assert history["messages"] == [] and history["total_messages"] == 0
    conversation.refresh_from_db()
    assert conversation.history_mode == "normalized"
    assert conversation.messages.count() == 0


def test_explicit_host_json_compatibility_does_not_repin_existing_threads(
    world, monkeypatch, api_for, client_for,
):
    # Named legacy-host exception: only this test opts out of normalized storage.
    # New threads inherit JSON; already initialized conversations stay pinned.
    config = conf.AgentRuntimeSettings()
    config.MESSAGE_STORAGE_MODE = "json"
    monkeypatch.setattr(conf, "_settings_instance", config)
    thread = create_thread(world.owner, world.project, world.agent)
    conversation = AgentConversation.objects.get(pk=thread["conversation_id"])
    assert conversation.history_mode == "json" and conversation.history_state == "idle"
    assert conversation.history_version == 0
    assert AgentDefinition.objects.get(pk=world.agent["id"]).message_storage_mode == ""
    assert api_for(world.owner).get("studio", "threads", thread["id"])["id"] == thread["id"]
    history = client_for(world.owner).get(WORKSPACE + f"conversations/{conversation.pk}/")
    assert history["history_mode"] == "json" and history["messages"] == []
    original = AgentConversation.objects.get(pk=world.thread["conversation_id"])
    assert original.history_mode == "normalized" and original.history_state == "ready"


def test_guest_and_cross_membership_project_isolation(world, api_for):
    guest_api = api_for(world.guest)
    assert ids(guest_api.list("studio", "projects")) == {world.project["id"]}
    detail = guest_api.get("studio", "projects", world.project["id"])
    assert detail["can_manage"] is False and detail["can_create_thread"] is False
    for project in (world.hidden, world.other_project):
        with pytest.raises(CLIError) as denied:
            guest_api.get("studio", "projects", project["id"])
        assert denied.value.status == 404
    assert api_for(world.member).list("studio", "projects")["items"] == []
    # Explicit membership in a second organization grants that project only.
    project_member(world.outsider, world.other_project, world.guest)
    assert ids(guest_api.list("studio", "projects")) == {
        world.project["id"], world.other_project["id"],
    }
    assert ids(api_for(world.outsider).list("studio", "projects")) == {world.other_project["id"]}


def test_private_selected_and_project_shares_use_supported_acl_services(world, api_for):
    guest_api, admin_api = api_for(world.guest), api_for(world.admin)
    for api in (guest_api, admin_api, api_for(world.outsider)):
        assert_hidden(api, world.thread)
    path = f"threads/{world.thread['id']}/"
    world.owner.workspace("patch", path, {
        "visibility": "selected", "shared_user_ids": [world.guest.user.pk],
    }, status=200)
    shared = guest_api.get("studio", "threads", world.thread["id"])
    assert shared["can_write"] is False and shared["can_manage"] is False
    assert_hidden(admin_api, world.thread)
    assert StudioThread.objects.get(pk=world.thread["id"]).shares.filter(user=world.guest.user).exists()
    world.owner.workspace("patch", path, {"visibility": "project"}, status=200)
    assert admin_api.get("studio", "threads", world.thread["id"])["can_write"] is False
    assert_hidden(api_for(world.member), world.thread)
    world.owner.workspace("patch", path, {"visibility": "private"}, status=200)
    assert not StudioThread.objects.get(pk=world.thread["id"]).shares.exists()
    assert_hidden(guest_api, world.thread)
    assert_hidden(admin_api, world.thread)


@pytest.mark.parametrize("scope", ["project", "organization"])
def test_membership_revocation_removes_shared_thread_access(world, api_for, scope):
    project_member(world.owner, world.project, world.member)
    world.owner.workspace("patch", f"threads/{world.thread['id']}/", {
        "visibility": "selected", "shared_user_ids": [world.member.user.pk],
    }, status=200)
    api = api_for(world.member)
    assert api.get("studio", "threads", world.thread["id"])["can_write"] is False
    parent = world.project if scope == "project" else world.organization
    world.owner.workspace("delete", f"{scope}s/{parent['id']}/members/{world.member.user.pk}/", status=204)
    assert not StudioProjectMembership.objects.filter(
        project_id=world.project["id"], user=world.member.user,
    ).exists()
    assert not StudioThread.objects.get(pk=world.thread["id"]).shares.exists()
    assert_hidden(api, world.thread)


def test_creator_revocation_is_studio_overlay_not_runtime_ownership(
    world, api_for, queued_run,
):
    project_member(world.owner, world.project, world.member, role="editor")
    agent = create_agent(world.member, "contract-creator-agent")
    thread = create_thread(world.member, world.project, agent)
    run = queued_run(world.member, agent, thread=thread)
    api = api_for(world.member)
    assert api.get("studio", "threads", thread["id"])["can_write"] is True
    world.owner.workspace("delete", f"projects/{world.project['id']}/members/{world.member.user.pk}/", status=204)
    assert_hidden(api, thread)
    assert AgentConversation.objects.get(pk=thread["conversation_id"]).user_id == world.member.user.pk
    assert ids(api.list("runtime", "runs")) == {str(run.pk)}


def test_thread_pagination_and_filters_count_only_authorized_rows(world, api_for):
    second = create_thread(world.owner, world.project, world.agent, title="Page two")
    third = create_thread(world.owner, world.project, world.agent, title="Page three")
    other_agent = create_agent(world.outsider, "contract-pagination-agent")
    create_thread(world.outsider, world.other_project, other_agent)
    api = api_for(world.owner)
    query = {"project_id": world.project["id"], "limit": 1}
    first_page = api.list("studio", "threads", query=query)
    assert first_page["count"] == 3 and len(first_page["items"]) == 1
    assert first_page["has_more"] is True and first_page["pages"] == 1
    all_pages = api.list("studio", "threads", query=query, all_pages=True, max_pages=10)
    assert all_pages["count"] == 3 and all_pages["pages"] == 3
    assert all_pages["has_more"] is False
    assert ids(all_pages) == {world.thread["id"], second["id"], third["id"]}
    assert query == {"project_id": world.project["id"], "limit": 1}
    with pytest.raises(CLIError) as bounded:
        api.list("studio", "threads", query=query, all_pages=True, max_pages=1)
    assert bounded.value.code == "pagination"


def test_archived_filter_crosses_the_adapter_and_transport_boundary(world, api_for):
    archived = create_thread(world.owner, world.project, world.agent, title="Archived thread")
    world.owner.workspace("patch", f"threads/{archived['id']}/", {"archived": True}, status=200)
    api = api_for(world.owner)
    assert ids(api.list("studio", "threads", query={"archived": True})) == {archived["id"]}
    assert ids(api.list("studio", "threads", query={"archived": False})) == {world.thread["id"]}
