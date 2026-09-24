"""Messaging: channels, direct messages, threads, mentions and inboxes.

Postgres rows are the message bus: every message is durable and ordered by a
per-channel sequence. Agents are not always running, so delivery means an
``InboxItem``; a host may set ``WORKSPACE_ON_INBOX`` to wake the recipient agent
(after commit). Without that callback agents read their inbox when they next run.

Safety guards on agent authors (humans are never limited by them):
- a paused workspace rejects agent writes;
- ``WORKSPACE_AGENT_MESSAGES_PER_HOUR`` (default 60) per agent;
- ``WORKSPACE_MAX_AGENT_STREAK`` (default 20): after that many consecutive agent
  messages in one thread (or channel top level) a human must post before agents
  may continue. This stops agent-to-agent reply loops.
Message bodies are untrusted content from other principals, not instructions.
"""

import hashlib
import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.module_loading import import_string

from . import identity
from .errors import Blocked, Conflict, InvalidRequest, NotFound
from .models import Channel, ChannelMember, InboxItem, Membership, Message
from .services import (
    _require_active,
    check_receipt,
    get_membership,
    record,
    store_receipt,
)

MAX_BODY = 20_000
MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_-]{0,39})")


def _setting(name, default):
    return getattr(settings, name, default)


# --------------------------------------------------------------------------- access

def _channel_for(*, actor, channel_id, write=False, lock=False):
    """Return (channel, membership) if the actor may read (or write) it."""
    actor = _require_active(actor)
    channels = Channel.objects.select_for_update() if lock else Channel.objects
    channel = channels.filter(pk=channel_id).first()
    if channel is None:
        raise NotFound()
    role = Membership.Role.MEMBER if write else Membership.Role.VIEWER
    try:
        membership = get_membership(actor=actor, workspace_id=channel.workspace_id, role=role)
    except NotFound:
        raise NotFound() from None
    is_member = ChannelMember.objects.filter(channel=channel, principal=actor).exists()
    if channel.kind != Channel.Kind.PUBLIC and not is_member:
        raise NotFound()
    return channel, membership


def _check_agent_guards(*, actor, workspace, channel, thread_root):
    if not identity.is_agent(actor):
        return
    if workspace.is_paused:
        raise Blocked("Workspace is paused.")
    hourly = int(_setting("WORKSPACE_AGENT_MESSAGES_PER_HOUR", 60))
    since = timezone.now() - timedelta(hours=1)
    if Message.objects.filter(author=actor, created_at__gte=since).count() >= hourly:
        raise Blocked("Hourly message limit reached.")
    streak_limit = int(_setting("WORKSPACE_MAX_AGENT_STREAK", 20))
    recent = (Message.objects.filter(channel=channel, thread_root=thread_root)
              .order_by("-seq").values_list("author_is_agent", flat=True)[:streak_limit])
    if len(recent) >= streak_limit and all(recent):
        raise Blocked("Agents have posted too many messages in a row; a human must reply first.")


# --------------------------------------------------------------------------- channels

def create_channel(*, actor, workspace_id, name, kind=Channel.Kind.PUBLIC, topic="",
                   idempotency_key):
    if kind not in (Channel.Kind.PUBLIC, Channel.Kind.PRIVATE):
        raise InvalidRequest(fields={"kind": "Use public or private; use open_direct for DMs."})
    name = (name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", name):
        raise InvalidRequest(fields={"name": "Lowercase letters, digits, - and _, at most 80."})
    if len(topic) > 300:
        raise InvalidRequest(fields={"topic": "At most 300 characters."})
    payload = {"workspace": str(workspace_id), "name": name, "kind": kind, "topic": topic}
    with transaction.atomic():
        membership = get_membership(actor=actor, workspace_id=workspace_id,
                                    role=Membership.Role.MEMBER, lock=True)
        actor = membership.principal
        replay = check_receipt(actor=actor, key=idempotency_key, command="create_channel",
                               payload=payload, model=Channel)
        if replay is not None:
            return replay
        if identity.is_agent(actor) and membership.workspace.is_paused:
            raise Blocked("Workspace is paused.")
        try:
            with transaction.atomic():
                channel = Channel.objects.create(workspace=membership.workspace, kind=kind,
                                                 name=name, topic=topic, created_by=actor)
        except IntegrityError as exc:
            raise Conflict("A channel with that name already exists.") from exc
        ChannelMember.objects.create(channel=channel, principal=actor)
        record(workspace=membership.workspace, actor=actor, verb="channel.created",
               target=channel, data={"kind": kind})
        store_receipt(actor=actor, key=idempotency_key, command="create_channel",
                      payload=payload, result=channel)
    return channel


def open_direct(*, actor, workspace_id, other):
    """Return the single DM channel between two workspace members, creating it once."""
    other = identity.parse(other, field="other")
    with transaction.atomic():
        membership = get_membership(actor=actor, workspace_id=workspace_id,
                                    role=Membership.Role.MEMBER, lock=True)
        actor = membership.principal
        if other == actor:
            raise InvalidRequest(fields={"other": "Cannot message yourself."})
        if (not Membership.objects.filter(workspace=membership.workspace, principal=other).exists()
                or not identity.is_active(other)):
            raise NotFound()
        key = hashlib.sha256("\n".join(sorted([actor, other])).encode()).hexdigest()
        channel = Channel.objects.filter(workspace=membership.workspace, kind=Channel.Kind.DIRECT,
                                         direct_key=key).first()
        if channel is None:
            channel = Channel.objects.create(workspace=membership.workspace, kind=Channel.Kind.DIRECT,
                                             direct_key=key, created_by=actor)
            ChannelMember.objects.bulk_create([ChannelMember(channel=channel, principal=actor),
                                               ChannelMember(channel=channel, principal=other)])
            record(workspace=membership.workspace, actor=actor, verb="direct.opened", target=channel)
    return channel


def add_to_channel(*, actor, channel_id, principal):
    """Add a workspace member to a private channel. Existing channel members only."""
    principal = identity.parse(principal)
    with transaction.atomic():
        channel, membership = _channel_for(actor=actor, channel_id=channel_id, write=True, lock=True)
        if channel.kind == Channel.Kind.DIRECT:
            raise InvalidRequest(fields={"channel": "Direct messages have fixed members."})
        if not Membership.objects.filter(workspace_id=channel.workspace_id,
                                         principal=principal).exists():
            raise NotFound()
        ChannelMember.objects.get_or_create(channel=channel, principal=principal)
        record(workspace=membership.workspace, actor=membership.principal,
               verb="channel.member_added", target=channel)
    return channel


def list_channels(*, actor, workspace_id):
    """Public channels plus private channels and DMs the actor belongs to."""
    membership = get_membership(actor=actor, workspace_id=workspace_id)
    joined = ChannelMember.objects.filter(principal=membership.principal).values("channel_id")
    return list(Channel.objects.filter(workspace=membership.workspace)
                .filter(kind=Channel.Kind.PUBLIC).union(
                    Channel.objects.filter(workspace=membership.workspace, pk__in=joined))
                .order_by("kind", "name"))


# --------------------------------------------------------------------------- messages

def post_message(*, actor, channel_id, body, thread_root_id=None, idempotency_key):
    body = body if isinstance(body, str) else ""
    if not body.strip() or len(body) > MAX_BODY:
        raise InvalidRequest(fields={"body": f"Required, at most {MAX_BODY} characters."})
    payload = {"channel": str(channel_id), "body": body, "thread": str(thread_root_id or "")}
    inbox_ids = []
    with transaction.atomic():
        # Lock the channel first: serializes sequence allocation and guard checks.
        channel, membership = _channel_for(actor=actor, channel_id=channel_id, write=True, lock=True)
        actor = membership.principal
        replay = check_receipt(actor=actor, key=idempotency_key, command="post_message",
                               payload=payload, model=Message)
        if replay is not None:
            return replay
        root = None
        if thread_root_id is not None:
            root = Message.objects.filter(pk=thread_root_id, channel=channel).first()
            if root is None:
                raise NotFound()
            if root.thread_root_id is not None:
                root = root.thread_root  # replies to replies join the same thread
        _check_agent_guards(actor=actor, workspace=membership.workspace, channel=channel,
                            thread_root=root)
        channel.last_seq += 1
        channel.save(update_fields=["last_seq"])
        message = Message.objects.create(channel=channel, seq=channel.last_seq, author=actor,
                                         author_is_agent=identity.is_agent(actor),
                                         thread_root=root, body=body)
        if root is not None:
            Message.objects.filter(pk=root.pk).update(reply_count=F("reply_count") + 1)
        member, _ = ChannelMember.objects.get_or_create(channel=channel, principal=actor)
        member.last_read_seq = channel.last_seq
        member.save(update_fields=["last_read_seq"])
        inbox_ids = _deliver(channel=channel, message=message, root=root, author=actor)
        record(workspace=membership.workspace, actor=actor, verb="message.posted", target=message,
               data={"channel": str(channel.pk), "thread": str(root.pk) if root else ""})
        store_receipt(actor=actor, key=idempotency_key, command="post_message",
                      payload=payload, result=message)
    if inbox_ids:
        _notify(inbox_ids)
    return message


def _deliver(*, channel, message, root, author):
    """Create inbox items for recipients who can read the channel."""
    reasons = {}
    readers = Membership.objects.filter(workspace_id=channel.workspace_id)
    if channel.kind != Channel.Kind.PUBLIC:
        readers = readers.filter(principal__in=ChannelMember.objects
                                 .filter(channel=channel).values("principal"))
    handles = set(MENTION.findall(message.body.lower()))
    if handles:
        for principal in readers.filter(handle__in=handles).values_list("principal", flat=True):
            reasons[principal] = InboxItem.Reason.MENTION
    if channel.kind == Channel.Kind.DIRECT:
        for principal in readers.values_list("principal", flat=True):
            reasons.setdefault(principal, InboxItem.Reason.DIRECT)
    if root is not None:
        participants = set(Message.objects.filter(thread_root=root)
                           .values_list("author", flat=True)) | {root.author}
        for principal in readers.filter(principal__in=participants).values_list(
                "principal", flat=True):
            reasons.setdefault(principal, InboxItem.Reason.REPLY)
    reasons.pop(author, None)
    # Deactivated agents/users get nothing (and a wake-up hook is never called for them).
    items = InboxItem.objects.bulk_create(
        [InboxItem(recipient=p, message=message, reason=reason)
         for p, reason in reasons.items() if identity.is_active(p)]
    )
    return [item.pk for item in items]


def _notify(inbox_ids):
    path = _setting("WORKSPACE_ON_INBOX", None)
    if not path:
        return
    callback = import_string(path) if isinstance(path, str) else path
    # After commit only, so a woken agent always finds the committed message.
    transaction.on_commit(lambda: callback(inbox_item_ids=list(inbox_ids)))


def list_messages(*, actor, channel_id, after_seq=0, thread_root_id=None, limit=50):
    """Messages in order. Top level by default, or one thread's replies."""
    channel, _ = _channel_for(actor=actor, channel_id=channel_id)
    limit = max(1, min(int(limit), 100))
    messages = Message.objects.filter(channel=channel, seq__gt=max(0, int(after_seq)))
    if thread_root_id is None:
        messages = messages.filter(thread_root__isnull=True)
    else:
        messages = messages.filter(thread_root_id=thread_root_id)
    return list(messages.order_by("seq")[:limit])


def mark_read(*, actor, channel_id, seq):
    """Advance the actor's read cursor (never backwards) and clear matching inbox items."""
    with transaction.atomic():
        channel, membership = _channel_for(actor=actor, channel_id=channel_id)
        seq = max(0, min(int(seq), channel.last_seq))
        member, _ = ChannelMember.objects.get_or_create(channel=channel, principal=membership.principal)
        if seq > member.last_read_seq:
            member.last_read_seq = seq
            member.save(update_fields=["last_read_seq"])
        InboxItem.objects.filter(recipient=membership.principal, message__channel=channel,
                                 message__seq__lte=seq, read_at__isnull=True).update(
            read_at=timezone.now())
    return member.last_read_seq


def list_inbox(*, actor, unread_only=True, limit=50):
    """The actor's inbox across workspaces; items it can no longer read are hidden."""
    actor = _require_active(actor)
    limit = max(1, min(int(limit), 100))
    items = InboxItem.objects.filter(
        recipient=actor, message__channel__workspace__memberships__principal=actor,
    ).select_related("message", "message__channel").distinct()
    if unread_only:
        items = items.filter(read_at__isnull=True)
    visible = []
    for item in items.order_by("-id")[: limit * 2]:
        channel = item.message.channel
        if (channel.kind == Channel.Kind.PUBLIC
                or ChannelMember.objects.filter(channel=channel, principal=actor).exists()):
            visible.append(item)
        if len(visible) == limit:
            break
    return visible
