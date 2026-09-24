"""Agent email: inboxes, send, reply, read. The agent's own 'Gmail'.

Sends are idempotent end to end: our ``ExternalMessage`` row is keyed by the
caller's idempotency key, and the same key is passed to AgentMail's
``Idempotency-Key`` header, so a retry after a timeout never sends twice.
"""

import re
import uuid

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import policy as policy_mod
from .errors import (
    InvalidRequest,
    NotFound,
    PermissionDenied,
    ProviderError,
    RateLimited,
)
from .models import Domain, Endpoint, ExternalMessage, OptOut
from .providers import config, email_provider

EMAIL_RE = re.compile(r"^[^@\s<>\"]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,63}$", re.I)
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
MAX_RECIPIENTS = 50
MAX_BODY = 100_000
# Daily sends allowed per purchased domain, by day since verification (then unlimited
# up to the host cap). Protects new domains' reputation.
WARMUP = (20, 40, 80, 150, 300, 500, 800)


def _addresses(value, field):
    items = [value] if isinstance(value, str) else list(value or [])
    out = []
    for item in items:
        item = str(item).strip().lower()
        if not EMAIL_RE.fullmatch(item):
            raise InvalidRequest(fields={field: f"Not an email address: {item[:80]}"})
        out.append(item)
    return out


def create_inbox(*, agent, username, domain=None, display_name=""):
    """Create an inbox for the agent on its own ready domain or the host default."""
    key, _ = policy_mod.require(agent, "email")
    username = (username or "").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise InvalidRequest(fields={"username": "Letters, digits, . _ - (max 63)."})
    domain_row = None
    domain_name = (domain or config("DEFAULT_EMAIL_DOMAIN") or "").strip().lower() or None
    if domain_name:
        domain_row = Domain.objects.filter(name=domain_name).first()
        if domain_row is None:
            raise NotFound()
        if domain_row.owner_agent not in ("", key):
            raise PermissionDenied("That domain belongs to another agent.")
        if domain_row.status != Domain.Status.READY:
            raise InvalidRequest(fields={"domain": "Domain is not verified for email yet."})
    address = f"{username}@{domain_name or 'agentmail.to'}"
    existing = Endpoint.objects.filter(address=address).first()
    if existing is not None:
        if existing.agent != key:
            raise InvalidRequest(fields={"username": "That address is taken."})
        return existing
    inbox = email_provider().create_inbox(
        username=username, domain=domain_name, display_name=(display_name or "")[:200],
        client_id=f"inbox-{key.replace(':', '-')}-{username}-{domain_name or 'default'}")
    endpoint, _ = Endpoint.objects.get_or_create(
        address=inbox.address,
        defaults={"agent": key, "kind": Endpoint.Kind.EMAIL, "provider": "agentmail",
                  "provider_id": inbox.provider_id, "domain": domain_row,
                  "display_name": (display_name or "")[:200]})
    policy_mod.audit(agent=key, actor=key, verb="email.inbox_created", target=endpoint.address)
    return endpoint


def list_inboxes(*, agent):
    return list(Endpoint.objects.filter(agent=agent, kind=Endpoint.Kind.EMAIL, is_active=True))


def _endpoint(key, inbox):
    """The agent's active inbox, by address or endpoint id. Others' inboxes are NotFound."""
    active = Endpoint.objects.filter(kind=Endpoint.Kind.EMAIL, is_active=True)
    endpoint = (active.filter(pk=inbox).first() if _is_uuid(inbox)
                else active.filter(address=str(inbox).strip().lower()).first())
    if endpoint is None or endpoint.agent != key:
        raise NotFound()
    return endpoint


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _check_warmup(endpoint, exclude=None):
    """Raise if today's sends on a new domain already reached its warm-up cap."""
    domain = endpoint.domain
    if domain is None or not domain.purchased or domain.warmup_started_at is None:
        return
    day = (timezone.now() - domain.warmup_started_at).days
    if day >= len(WARMUP):
        return
    today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    sent = ExternalMessage.objects.filter(endpoint__domain=domain, direction=ExternalMessage.Direction.OUT,
                                          created_at__gte=today).exclude(
        status__in=[ExternalMessage.Status.FAILED, ExternalMessage.Status.PENDING_APPROVAL,
                    ExternalMessage.Status.REJECTED]).exclude(pk=exclude).count()
    if sent >= WARMUP[day]:
        raise RateLimited(f"New domain warm-up: {WARMUP[day]} emails/day on day {day + 1}. Try tomorrow.")


def send_email(*, agent, inbox, body, idempotency_key, to=(), subject="", cc=(), bcc=(),
               reply_to_message_id=None):
    key, policy = policy_mod.require(agent, "email")
    endpoint = _endpoint(key, inbox)
    to = _addresses(to, "to") if not reply_to_message_id else []
    cc, bcc = _addresses(cc, "cc"), _addresses(bcc, "bcc")
    if not reply_to_message_id and not to:
        raise InvalidRequest(fields={"to": "At least one recipient."})
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise InvalidRequest(fields={"to": f"At most {MAX_RECIPIENTS} recipients in total."})
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY:
        raise InvalidRequest(fields={"body": f"Required, at most {MAX_BODY} characters."})
    subject = (subject or "")[:998]
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
        raise InvalidRequest(fields={"idempotency_key": "Required, at most 200 characters."})
    parent = None
    if reply_to_message_id:
        parent = ExternalMessage.objects.filter(endpoint=endpoint, pk=reply_to_message_id).first() \
            if _is_uuid(reply_to_message_id) else None
        if parent is None:
            raise NotFound()
    if OptOut.objects.filter(kind="email", address__in=to + cc + bcc).exists():
        raise PermissionDenied("A recipient has opted out of email from agents.")
    if not policy.require_approval:
        _check_warmup(endpoint)  # before recording, so a refused send leaves no row
    with transaction.atomic():
        try:
            with transaction.atomic():
                message = ExternalMessage.objects.create(
                    endpoint=endpoint, direction=ExternalMessage.Direction.OUT,
                    status=(ExternalMessage.Status.PENDING_APPROVAL if policy.require_approval
                            else ExternalMessage.Status.QUEUED),
                    counterparty=(to or [parent.counterparty])[0], recipients=to + cc + bcc,
                    subject=subject, body=body, dedupe_key=idempotency_key,
                    thread_ref=parent.thread_ref if parent else "",
                    in_reply_to=parent.provider_message_id if parent else "")
        except IntegrityError:
            message = ExternalMessage.objects.get(endpoint=endpoint, direction="out",
                                                  dedupe_key=idempotency_key)
            if message.body != body or message.subject != subject:
                raise InvalidRequest(fields={"idempotency_key": "Already used for a different email."}) from None
            if message.status not in (ExternalMessage.Status.QUEUED,):
                return message
        else:
            if message.status == ExternalMessage.Status.PENDING_APPROVAL:
                policy_mod.audit(agent=key, actor=key, verb="email.awaiting_approval", target=endpoint.address,
                                 data={"message": str(message.pk)})
                return message
    return _deliver(message, cc=cc, bcc=bcc)


def _deliver(message, *, cc=(), bcc=()):
    endpoint = message.endpoint
    provider = email_provider()
    ExternalMessage.objects.filter(pk=message.pk).update(attempts=message.attempts + 1)
    try:
        if message.in_reply_to:
            sent = provider.reply(inbox_id=endpoint.provider_id, message_id=message.in_reply_to,
                                  text=message.body, idempotency_key=f"msg-{message.pk}")
        else:
            to = [r for r in message.recipients if r not in set(cc) | set(bcc)] or message.recipients
            sent = provider.send(inbox_id=endpoint.provider_id, to=to, subject=message.subject,
                                 text=message.body, cc=list(cc), bcc=list(bcc),
                                 idempotency_key=f"msg-{message.pk}")
    except ProviderError as exc:
        if not exc.retriable:
            message.status = ExternalMessage.Status.FAILED
            message.error = exc.message[:300]
            message.save(update_fields=["status", "error", "updated_at"])
        # Retriable: stays QUEUED; retrying with the same key reuses the provider key (no double send).
        policy_mod.audit(agent=endpoint.agent, verb="email.send_error", target=endpoint.address,
                         data={"message": str(message.pk), "retriable": exc.retriable})
        raise
    message.status = ExternalMessage.Status.SENT
    message.provider_message_id = sent.provider_message_id
    # A reply stays in its parent's thread; a new message takes the provider's thread id.
    message.thread_ref = message.thread_ref or sent.thread_ref
    message.save(update_fields=["status", "provider_message_id", "thread_ref", "updated_at"])
    policy_mod.audit(agent=endpoint.agent, actor=endpoint.agent, verb="email.sent", target=endpoint.address,
                     data={"message": str(message.pk), "recipients": len(message.recipients)})
    return message


def approve(*, user, message_id, approve=True):
    """Owner approves/rejects an email held by the opt-in approval mode."""
    message = ExternalMessage.objects.select_related("endpoint").filter(
        pk=message_id, status=ExternalMessage.Status.PENDING_APPROVAL).first()
    if message is None or not policy_mod.identity.can_manage(user, message.endpoint.agent):
        raise NotFound()
    if not approve:
        message.status = ExternalMessage.Status.REJECTED
        message.save(update_fields=["status", "updated_at"])
        return message
    if message.endpoint.kind == Endpoint.Kind.EMAIL:
        _check_warmup(message.endpoint, exclude=message.pk)
    message.status = ExternalMessage.Status.QUEUED
    message.save(update_fields=["status", "updated_at"])
    policy_mod.audit(agent=message.endpoint.agent, actor=f"user:{user.pk}", verb="email.approved",
                     target=message.endpoint.address, data={"message": str(message.pk)})
    if message.endpoint.kind == Endpoint.Kind.SMS:
        from .sms import _deliver as deliver_sms
        return deliver_sms(message)
    return _deliver(message)


def list_messages(*, agent, inbox, direction=None, limit=25, before=None):
    key = policy_mod.identity.require_agent(agent)
    endpoint = _endpoint(key, inbox)
    messages = ExternalMessage.objects.filter(endpoint=endpoint)
    if direction in ("in", "out"):
        messages = messages.filter(direction=direction)
    if before:
        messages = messages.filter(created_at__lt=before)
    return list(messages.order_by("-created_at")[:max(1, min(int(limit), 100))])


def get_thread(*, agent, message_id):
    key = policy_mod.identity.require_agent(agent)
    message = ExternalMessage.objects.select_related("endpoint").filter(pk=message_id).first() \
        if _is_uuid(message_id) else None
    if message is None or message.endpoint.agent != key:
        raise NotFound()
    if not message.thread_ref:
        return [message]
    return list(ExternalMessage.objects.filter(endpoint=message.endpoint, thread_ref=message.thread_ref)
                .order_by("created_at"))

