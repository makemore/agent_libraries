"""Channels data model. Write only through ``services``.

Agents are canonical runtime principals (``agent:<uuid>``), stored as strings.
Money is integer US cents. Provider credentials are never stored here: they come
from host settings/secret storage and are held by the server only.
"""

import uuid

from django.db import models
from django.db.models import Q

PRINCIPAL_MAX = 255


class AgentChannelPolicy(models.Model):
    """What one agent may do on its own. Set by the agent's owner (or host)."""

    agent = models.CharField(max_length=PRINCIPAL_MAX, unique=True)
    enabled = models.BooleanField(default=False)  # master switch for this agent
    can_email = models.BooleanField(default=False)
    can_buy_domains = models.BooleanField(default=False)
    can_sms = models.BooleanField(default=False)
    # Opt-in approval-first mode; off means the agent acts without asking.
    require_approval = models.BooleanField(default=False)
    monthly_limit_cents = models.PositiveIntegerField(default=0)
    max_domain_price_cents = models.PositiveIntegerField(null=True, blank=True)
    allowed_tlds = models.JSONField(default=list, blank=True)  # empty = any the registrar supports
    auto_renew = models.BooleanField(default=False)
    updated_by = models.CharField(max_length=PRINCIPAL_MAX, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class SpendEntry(models.Model):
    """Ledger of money committed by an agent. Reserved before the provider call."""

    class State(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        CHARGED = "charged", "Charged"
        RELEASED = "released", "Released"  # provider call definitely failed

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.CharField(max_length=PRINCIPAL_MAX, db_index=True)
    kind = models.CharField(max_length=40)  # domain.register, sms.number, sms.message
    description = models.CharField(max_length=300)
    amount_cents = models.PositiveIntegerField()
    state = models.CharField(max_length=10, choices=State.choices, default=State.RESERVED)
    # Caller idempotency key: one purchase per (agent, key), so retries never buy twice.
    dedupe_key = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["agent", "dedupe_key"], name="channels_spend_once"),
        ]


class Domain(models.Model):
    class Status(models.TextChoices):
        REGISTERING = "registering", "Registering"
        DNS = "dns", "Configuring DNS"
        VERIFYING = "verifying", "Verifying email"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"
        ACTION_REQUIRED = "action_required", "Action required"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=253, unique=True)
    owner_agent = models.CharField(max_length=PRINCIPAL_MAX, blank=True)  # blank = host default
    purchased = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices)
    price_cents = models.PositiveIntegerField(default=0)
    renewal_cents = models.PositiveIntegerField(default=0)
    zone_id = models.CharField(max_length=64, blank=True)
    email_domain_id = models.CharField(max_length=128, blank=True)
    spend = models.ForeignKey(SpendEntry, null=True, blank=True, on_delete=models.PROTECT)
    detail = models.CharField(max_length=300, blank=True)
    # Sending ramp for a new domain: daily cap grows from this start date.
    warmup_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Endpoint(models.Model):
    """An address an agent owns: an email inbox or a phone number."""

    class Kind(models.TextChoices):
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.CharField(max_length=PRINCIPAL_MAX, db_index=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    address = models.CharField(max_length=320, unique=True)  # email or E.164
    provider = models.CharField(max_length=40)
    provider_id = models.CharField(max_length=128)
    domain = models.ForeignKey(Domain, null=True, blank=True, on_delete=models.PROTECT)
    display_name = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    spend = models.ForeignKey(SpendEntry, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class ExternalMessage(models.Model):
    """One inbound or outbound email/SMS. Inbound content is untrusted."""

    class Direction(models.TextChoices):
        IN = "in", "Inbound"
        OUT = "out", "Outbound"

    class Status(models.TextChoices):
        PENDING_APPROVAL = "pending_approval", "Awaiting approval"
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        BOUNCED = "bounced", "Bounced"
        RECEIVED = "received", "Received"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    endpoint = models.ForeignKey(Endpoint, on_delete=models.CASCADE, related_name="messages")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    status = models.CharField(max_length=20, choices=Status.choices)
    counterparty = models.CharField(max_length=320)  # sender (in) or first recipient (out)
    recipients = models.JSONField(default=list, blank=True)
    subject = models.CharField(max_length=998, blank=True)
    body = models.TextField(blank=True)
    thread_ref = models.CharField(max_length=256, blank=True)  # provider thread id
    in_reply_to = models.CharField(max_length=256, blank=True)
    provider_message_id = models.CharField(max_length=256, blank=True)
    # Caller idempotency (outbound) or provider event id (inbound).
    dedupe_key = models.CharField(max_length=256)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["endpoint", "direction", "dedupe_key"],
                                    name="channels_message_dedupe"),
        ]
        indexes = [
            models.Index(fields=["endpoint", "direction", "created_at"]),
            models.Index(fields=["provider_message_id"]),
        ]


class OptOut(models.Model):
    """Recipients who asked not to be contacted (e.g. SMS STOP)."""

    kind = models.CharField(max_length=10, choices=Endpoint.Kind.choices)
    address = models.CharField(max_length=320)
    source = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "address"], name="channels_optout_once"),
        ]


class ChannelEvent(models.Model):
    """Metadata-only audit log of every action (never message bodies or secrets)."""

    id = models.BigAutoField(primary_key=True)
    agent = models.CharField(max_length=PRINCIPAL_MAX, blank=True, db_index=True)
    actor = models.CharField(max_length=PRINCIPAL_MAX, blank=True)
    verb = models.CharField(max_length=60)
    target = models.CharField(max_length=320, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class WebhookReceipt(models.Model):
    """Provider event ids already processed, so redelivered webhooks are no-ops."""

    provider = models.CharField(max_length=40)
    event_id = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="channels_webhook_once"),
            models.CheckConstraint(condition=~Q(event_id=""), name="channels_webhook_event_id"),
        ]
