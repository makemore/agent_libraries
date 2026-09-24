"""Process verified provider webhooks: inbound messages and delivery status.

Every event is recorded once (``WebhookReceipt``), so provider redelivery is a
no-op. Inbound content is untrusted: it is stored and handed to the owning agent
as data, never as instructions, and can't change policy or trigger spending.

Host hook ``AGENT_CHANNELS["ON_INBOUND"]`` (callable/dotted path, called after
commit with ``message_id=``) can wake the agent or post to its workspace inbox.
"""

from django.db import IntegrityError, transaction
from django.utils.module_loading import import_string

from . import domains as domains_mod
from . import policy as policy_mod
from . import sms as sms_mod
from .models import Domain, Endpoint, ExternalMessage, WebhookReceipt
from .providers import InboundMessage, StatusUpdate, config

STATUS_ORDER = {"queued": 0, "sent": 1, "delivered": 2, "bounced": 3, "failed": 3}


def _first_time(provider, event_id):
    if not event_id:
        return True
    try:
        with transaction.atomic():
            WebhookReceipt.objects.create(provider=provider, event_id=event_id[:256])
        return True
    except IntegrityError:
        return False


def process(provider, items):
    """Apply normalised webhook items. Returns the number of new items applied."""
    applied = 0
    for item in items:
        with transaction.atomic():
            if not _first_time(provider, item.event_id):
                continue
            if isinstance(item, InboundMessage):
                applied += _inbound(provider, item)
            elif isinstance(item, StatusUpdate):
                applied += _status(item)
    return applied


def _inbound(provider, item):
    kind = Endpoint.Kind.SMS if provider == "twilio" else Endpoint.Kind.EMAIL
    address = item.to_address.lower() if kind == Endpoint.Kind.EMAIL else item.to_address
    endpoint = Endpoint.objects.filter(address=address, kind=kind, is_active=True).first()
    if endpoint is None:
        policy_mod.audit(verb="inbound.unrouted", data={"provider": provider})
        return 0
    if kind == Endpoint.Kind.SMS and sms_mod.handle_keywords(item.from_address, item.body):
        return 1
    parent = None
    if item.thread_ref:
        parent = ExternalMessage.objects.filter(endpoint=endpoint, thread_ref=item.thread_ref).first()
    message, created = ExternalMessage.objects.get_or_create(
        endpoint=endpoint, direction=ExternalMessage.Direction.IN,
        dedupe_key=(item.provider_message_id or item.event_id)[:256],
        defaults={"status": ExternalMessage.Status.RECEIVED, "counterparty": item.from_address,
                  "recipients": item.extra.get("to", [endpoint.address]), "subject": item.subject,
                  "body": item.body, "thread_ref": item.thread_ref or (parent.thread_ref if parent else ""),
                  "in_reply_to": item.in_reply_to, "provider_message_id": item.provider_message_id})
    if not created:
        return 0
    policy_mod.audit(agent=endpoint.agent, verb=f"{kind}.received", target=endpoint.address,
                     data={"message": str(message.pk)})
    hook = config("ON_INBOUND")
    if hook:
        callback = import_string(hook) if isinstance(hook, str) else hook
        transaction.on_commit(lambda: callback(message_id=message.pk))
    return 1


def _status(item):
    if not item.provider_message_id:
        return 0
    message = ExternalMessage.objects.filter(provider_message_id=item.provider_message_id,
                                             direction=ExternalMessage.Direction.OUT).first()
    if message is None:
        return 0
    if STATUS_ORDER.get(item.status, 0) < STATUS_ORDER.get(message.status, 0):
        return 0  # out-of-order webhook; never move status backwards
    message.status = item.status
    if item.status in ("bounced", "failed"):
        message.error = f"Provider reported {item.status}."
    message.save(update_fields=["status", "error", "updated_at"])
    return 1


def domain_verified(email_domain_id):
    row = Domain.objects.filter(email_domain_id=email_domain_id).first()
    if row is not None:
        domains_mod.mark_verified(row)
