"""Workspace data model. Write only through ``services`` and ``messaging``.

Principals are canonical runtime identity strings (``agent:<uuid>`` / ``user:<pk>``,
see ``identity``), stored without foreign keys so one identity serves every package.
"""

import uuid

from django.db import models
from django.db.models import Q

PRINCIPAL_MAX = 255


def principal_field(**kwargs):
    return models.CharField(max_length=PRINCIPAL_MAX, **kwargs)


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_by = principal_field()
    # When paused, agent principals cannot write; humans still can.
    is_paused = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        MEMBER = "member", "Member"
        MANAGER = "manager", "Manager"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    principal = principal_field(db_index=True)
    role = models.CharField(max_length=10, choices=Role.choices)
    handle = models.SlugField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "principal"], name="workspace_member_once"),
            models.UniqueConstraint(fields=["workspace", "handle"], name="workspace_handle_unique"),
        ]


class Activity(models.Model):
    """Metadata-only audit trail; never stores message bodies."""

    id = models.BigAutoField(primary_key=True)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="activity")
    actor = principal_field()
    verb = models.CharField(max_length=60)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["workspace", "-id"])]


class IdempotencyReceipt(models.Model):
    """Binds a caller's idempotency key to one actor, command and payload."""

    id = models.BigAutoField(primary_key=True)
    actor = principal_field()
    key_digest = models.CharField(max_length=64)
    command = models.CharField(max_length=60)
    payload_digest = models.CharField(max_length=64)
    result_type = models.CharField(max_length=40)
    result_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["actor", "key_digest"], name="workspace_receipt_once"),
        ]


class Channel(models.Model):
    class Kind(models.TextChoices):
        PUBLIC = "public", "Public"      # any workspace member may read and join
        PRIVATE = "private", "Private"  # channel members only
        DIRECT = "direct", "Direct"      # exactly two members, fixed

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="channels")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    name = models.SlugField(max_length=80, blank=True)
    topic = models.CharField(max_length=300, blank=True)
    # SHA-256 of the sorted principal pair for direct channels, so each pair has one DM.
    direct_key = models.CharField(max_length=64, blank=True)
    last_seq = models.PositiveBigIntegerField(default=0)
    created_by = principal_field()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"], condition=~Q(kind="direct"),
                name="workspace_channel_name_unique",
            ),
            models.UniqueConstraint(
                fields=["workspace", "direct_key"], condition=Q(kind="direct"),
                name="workspace_direct_pair_unique",
            ),
        ]


class ChannelMember(models.Model):
    id = models.BigAutoField(primary_key=True)
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="members")
    principal = principal_field()
    last_read_seq = models.PositiveBigIntegerField(default=0)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["channel", "principal"], name="workspace_channel_member_once"),
        ]


class Message(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="messages")
    seq = models.PositiveBigIntegerField()
    author = principal_field()
    thread_root = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies",
    )
    # Denormalized from the author key; used by the agent loop guard.
    author_is_agent = models.BooleanField(default=False)
    body = models.TextField()
    reply_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["channel", "seq"], name="workspace_message_seq_unique"),
        ]
        indexes = [models.Index(fields=["author", "created_at"])]


class InboxItem(models.Model):
    """Something a principal should look at. Hosts may use it to wake an agent."""

    class Reason(models.TextChoices):
        MENTION = "mention", "Mention"
        DIRECT = "direct", "Direct message"
        REPLY = "reply", "Thread reply"

    id = models.BigAutoField(primary_key=True)
    recipient = principal_field(db_index=True)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=10, choices=Reason.choices)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "message"], name="workspace_inbox_once"),
        ]
        indexes = [models.Index(fields=["recipient", "read_at", "-id"])]
