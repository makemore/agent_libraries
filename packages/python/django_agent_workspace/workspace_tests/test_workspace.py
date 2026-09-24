import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django_agent_runtime.models import AgentDefinition
from django_agent_workspace import identity, messaging, services
from django_agent_workspace.errors import (
    Blocked,
    Conflict,
    InvalidRequest,
    NotFound,
    PermissionDenied,
)
from django_agent_workspace.models import Activity, InboxItem, Membership

pytestmark = pytest.mark.django_db


def allow_all(*, user, agent):
    return True


class Human(str):
    """A user principal key that also carries its user row for test setup."""


def human(name):
    user = get_user_model().objects.create_user(username=name, password=None)
    key = Human(identity.for_user(user))
    key.user = user
    return key


def agent(owner, name):
    definition = AgentDefinition.objects.create(owner=owner.user, name=name,
                                                slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}")
    return f"agent:{definition.pk}"


def key():
    return uuid.uuid4().hex


@override_settings(WORKSPACE_CAN_ADD_AGENT=allow_all)
def workspace_with(owner, *members, role=Membership.Role.MEMBER):
    ws = services.create_workspace(actor=owner, name="Team", idempotency_key=key())
    for member in members:
        ws = services.set_member(actor=owner, workspace_id=ws.pk, principal=member,
                                 role=role, expected_revision=ws.revision)
    return ws


# ------------------------------------------------------------------ identity

def test_members_are_runtime_principals():
    owner = human("alice")
    bot = agent(owner, "Researcher")
    ws = workspace_with(owner, bot)
    members = {m.principal: m.handle for m in services.list_members(actor=owner, workspace_id=ws.pk)}
    assert members == {f"user:{owner.user.pk}": "alice", bot: "researcher"}


def test_adding_agent_is_blocked_without_host_policy():
    owner = human("alice")
    bot = agent(owner, "Bot")
    ws = services.create_workspace(actor=owner, name="Team", idempotency_key=key())
    with pytest.raises(PermissionDenied):
        services.set_member(actor=owner, workspace_id=ws.pk, principal=bot, role="member",
                            expected_revision=ws.revision)


def test_agent_does_not_inherit_owner_access():
    owner = human("alice")
    bot = agent(owner, "Researcher")
    ws = workspace_with(owner)
    with pytest.raises(NotFound):
        services.list_members(actor=bot, workspace_id=ws.pk)


@pytest.mark.parametrize("value", ["team:ops", "agent", "user:", 42])
def test_non_member_kinds_are_rejected(value):
    owner = human("alice")
    ws = workspace_with(owner)
    with pytest.raises(InvalidRequest):
        services.set_member(actor=owner, workspace_id=ws.pk, principal=value, role="member",
                            expected_revision=ws.revision)


def test_unknown_agent_cannot_be_added_or_act():
    owner = human("alice")
    ghost = f"agent:{uuid.uuid4()}"
    ws = workspace_with(owner)
    with pytest.raises(NotFound), override_settings(WORKSPACE_CAN_ADD_AGENT=allow_all):
        services.set_member(actor=owner, workspace_id=ws.pk, principal=ghost, role="member",
                            expected_revision=ws.revision)
    with pytest.raises(PermissionDenied):
        services.list_workspaces(actor=ghost)


def test_deactivated_agent_loses_access_and_inbox():
    owner = human("alice")
    bot = agent(owner, "Researcher")
    ws = workspace_with(owner, bot)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    AgentDefinition.objects.filter(pk=bot.split(":", 1)[1]).update(is_active=False)
    with pytest.raises(PermissionDenied):
        messaging.list_messages(actor=bot, channel_id=channel.pk)
    messaging.post_message(actor=owner, channel_id=channel.pk, body="@researcher ping",
                           idempotency_key=key())
    assert not InboxItem.objects.filter(recipient=bot).exists()


# ------------------------------------------------------------------ workspaces

def test_create_workspace_is_idempotent_and_creator_is_manager():
    owner = human("alice")
    k = key()
    first = services.create_workspace(actor=owner, name="Team", idempotency_key=k)
    again = services.create_workspace(actor=owner, name="Team", idempotency_key=k)
    assert first.pk == again.pk
    assert Membership.objects.get(workspace=first, principal=owner).role == Membership.Role.MANAGER
    with pytest.raises(Conflict):
        services.create_workspace(actor=owner, name="Other", idempotency_key=k)


def test_non_members_cannot_see_workspace():
    ws = workspace_with(human("alice"))
    with pytest.raises(NotFound):
        services.list_members(actor=human("mallory"), workspace_id=ws.pk)


def test_set_member_requires_manager_and_current_revision():
    owner, bob, carol = human("alice"), human("bob"), human("carol")
    ws = workspace_with(owner, bob)
    with pytest.raises(PermissionDenied):
        services.set_member(actor=bob, workspace_id=ws.pk, principal=carol,
                            role="member", expected_revision=ws.revision)
    with pytest.raises(Conflict):
        services.set_member(actor=owner, workspace_id=ws.pk, principal=carol,
                            role="member", expected_revision=ws.revision - 1)


def test_last_manager_cannot_be_removed():
    owner = human("alice")
    ws = workspace_with(owner)
    with pytest.raises(Conflict):
        services.set_member(actor=owner, workspace_id=ws.pk, principal=owner,
                            role=None, expected_revision=ws.revision)


def test_duplicate_handle_conflicts():
    owner, bob, carol = human("alice"), human("bob"), human("carol")
    ws = workspace_with(owner)
    ws = services.set_member(actor=owner, workspace_id=ws.pk, principal=bob,
                             role="member", handle="dev", expected_revision=ws.revision)
    with pytest.raises(Conflict):
        services.set_member(actor=owner, workspace_id=ws.pk, principal=carol,
                            role="member", handle="dev", expected_revision=ws.revision)


def test_deactivated_user_loses_access():
    owner, bob = human("alice"), human("bob")
    ws = workspace_with(owner, bob)
    bob.user.is_active = False
    bob.user.save()
    with pytest.raises(PermissionDenied):
        services.list_members(actor=bob, workspace_id=ws.pk)


# ------------------------------------------------------------------ messaging

def test_post_message_mentions_and_idempotency():
    owner = human("alice")
    bot = agent(owner, "Researcher")
    ws = workspace_with(owner, bot)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    k = key()
    msg = messaging.post_message(actor=owner, channel_id=channel.pk,
                                 body="hey @researcher, look at this", idempotency_key=k)
    again = messaging.post_message(actor=owner, channel_id=channel.pk,
                                   body="hey @researcher, look at this", idempotency_key=k)
    assert msg.pk == again.pk and msg.seq == 1
    inbox = messaging.list_inbox(actor=bot)
    assert [(i.message_id, i.reason) for i in inbox] == [(msg.pk, InboxItem.Reason.MENTION)]
    assert not Activity.objects.filter(data__icontains="look at this").exists()


def test_direct_message_is_single_channel_and_private():
    owner, bob, carol = human("alice"), human("bob"), human("carol")
    ws = workspace_with(owner, bob, carol)
    dm = messaging.open_direct(actor=owner, workspace_id=ws.pk, other=bob)
    assert messaging.open_direct(actor=bob, workspace_id=ws.pk, other=owner).pk == dm.pk
    msg = messaging.post_message(actor=owner, channel_id=dm.pk, body="private", idempotency_key=key())
    assert [i.reason for i in messaging.list_inbox(actor=bob)] == [InboxItem.Reason.DIRECT]
    with pytest.raises(NotFound):
        messaging.list_messages(actor=carol, channel_id=dm.pk)
    assert msg.seq == 1


def test_private_channel_mentions_do_not_leak_to_non_members():
    owner, bob = human("alice"), human("bob")
    ws = workspace_with(owner, bob)
    secret = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="secret",
                                      kind="private", idempotency_key=key())
    messaging.post_message(actor=owner, channel_id=secret.pk, body="@bob hi", idempotency_key=key())
    assert messaging.list_inbox(actor=bob) == []


def test_thread_replies_notify_participants_and_mark_read():
    owner, bob = human("alice"), human("bob")
    ws = workspace_with(owner, bob)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    root = messaging.post_message(actor=owner, channel_id=channel.pk, body="plan?", idempotency_key=key())
    reply = messaging.post_message(actor=bob, channel_id=channel.pk, body="yes",
                                   thread_root_id=root.pk, idempotency_key=key())
    assert [i.reason for i in messaging.list_inbox(actor=owner)] == [InboxItem.Reason.REPLY]
    assert [m.pk for m in messaging.list_messages(actor=owner, channel_id=channel.pk)] == [root.pk]
    assert [m.pk for m in messaging.list_messages(actor=owner, channel_id=channel.pk,
                                                  thread_root_id=root.pk)] == [reply.pk]
    messaging.mark_read(actor=owner, channel_id=channel.pk, seq=reply.seq)
    assert messaging.list_inbox(actor=owner) == []


def test_list_channels_shows_public_and_own_private_only():
    owner, bob = human("alice"), human("bob")
    ws = workspace_with(owner, bob)
    public = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                      idempotency_key=key())
    private = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="secret",
                                       kind="private", idempotency_key=key())
    dm = messaging.open_direct(actor=owner, workspace_id=ws.pk, other=bob)
    assert {c.pk for c in messaging.list_channels(actor=owner, workspace_id=ws.pk)} == {
        public.pk, private.pk, dm.pk}
    assert {c.pk for c in messaging.list_channels(actor=bob, workspace_id=ws.pk)} == {public.pk, dm.pk}


def test_viewer_cannot_post():
    owner, bob = human("alice"), human("bob")
    ws = workspace_with(owner, bob, role=Membership.Role.VIEWER)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    with pytest.raises(PermissionDenied):
        messaging.post_message(actor=bob, channel_id=channel.pk, body="hi", idempotency_key=key())


def test_removed_member_loses_channel_access():
    owner, bob = human("alice"), human("bob")
    ws = workspace_with(owner, bob)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    services.set_member(actor=owner, workspace_id=ws.pk, principal=bob, role=None,
                        expected_revision=services.list_workspaces(actor=owner)[0].revision)
    with pytest.raises(NotFound):
        messaging.list_messages(actor=bob, channel_id=channel.pk)


# ------------------------------------------------------------------ safety guards (named exceptions)

def test_paused_workspace_blocks_agents_but_not_humans():
    owner = human("alice")
    bot = agent(owner, "Researcher")
    ws = workspace_with(owner, bot)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    services.set_paused(actor=owner, workspace_id=ws.pk, paused=True)
    with pytest.raises(Blocked):
        messaging.post_message(actor=bot, channel_id=channel.pk, body="hi", idempotency_key=key())
    messaging.post_message(actor=owner, channel_id=channel.pk, body="stop", idempotency_key=key())


@override_settings(WORKSPACE_MAX_AGENT_STREAK=4)
def test_agent_reply_loop_needs_a_human_to_continue():
    """Scenario override: small streak limit to exercise the loop guard quickly."""
    owner = human("alice")
    a, b = agent(owner, "Ping"), agent(owner, "Pong")
    ws = workspace_with(owner, a, b)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    for i in range(4):
        messaging.post_message(actor=(a, b)[i % 2], channel_id=channel.pk, body=f"{i}",
                               idempotency_key=key())
    with pytest.raises(Blocked):
        messaging.post_message(actor=a, channel_id=channel.pk, body="more", idempotency_key=key())
    messaging.post_message(actor=owner, channel_id=channel.pk, body="carry on", idempotency_key=key())
    messaging.post_message(actor=a, channel_id=channel.pk, body="ok", idempotency_key=key())


@override_settings(WORKSPACE_AGENT_MESSAGES_PER_HOUR=2)
def test_agent_hourly_limit():
    """Scenario override: tiny hourly limit to exercise the rate guard."""
    owner = human("alice")
    bot = agent(owner, "Chatty")
    ws = workspace_with(owner, bot)
    channel = messaging.create_channel(actor=owner, workspace_id=ws.pk, name="general",
                                       idempotency_key=key())
    for _ in range(2):
        messaging.post_message(actor=bot, channel_id=channel.pk, body="x", idempotency_key=key())
    with pytest.raises(Blocked):
        messaging.post_message(actor=bot, channel_id=channel.pk, body="x", idempotency_key=key())


calls = []


def record_wake(*, inbox_item_ids):
    calls.append(list(inbox_item_ids))


@override_settings(WORKSPACE_ON_INBOX="workspace_tests.test_workspace.record_wake")
def test_inbox_callback_runs_after_commit(django_capture_on_commit_callbacks):
    """Scenario override: host wake-up callback configured."""
    calls.clear()
    owner = human("alice")
    bot = agent(owner, "Researcher")
    ws = workspace_with(owner, bot)
    dm = messaging.open_direct(actor=owner, workspace_id=ws.pk, other=bot)
    with django_capture_on_commit_callbacks(execute=True):
        messaging.post_message(actor=owner, channel_id=dm.pk, body="task", idempotency_key=key())
    assert len(calls) == 1 and len(calls[0]) == 1
