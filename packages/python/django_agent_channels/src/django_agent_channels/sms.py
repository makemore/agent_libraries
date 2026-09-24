"""Agent SMS via Twilio: buy a number, send, receive, STOP/START handling.

Number purchases and messages are charged against the agent's spending limit
(host-configured price estimates; Twilio bills the account). A number purchase
is tagged with a unique label, so a timed-out purchase is recovered by lookup,
never bought twice. Twilio has no send idempotency key: a send that timed out
stays ``queued`` with an unknown outcome and is not re-sent automatically.
"""

import re

from django.db import IntegrityError, transaction

from . import policy as policy_mod
from .errors import InvalidRequest, NotFound, PermissionDenied, ProviderError
from .models import Endpoint, ExternalMessage, OptOut
from .providers import config, require, sms_provider

E164 = re.compile(r"^\+[1-9]\d{6,14}$")
MAX_SMS = 1600
STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
START_WORDS = {"start", "unstop", "yes"}


def _number(value, field):
    value = re.sub(r"[\s().-]", "", str(value or ""))
    if not E164.fullmatch(value):
        raise InvalidRequest(fields={field: "Phone number in E.164 form, e.g. +14155550123."})
    return value


def _webhook_url(path):
    return require("PUBLIC_BASE_URL").rstrip("/") + config("WEBHOOK_PATH", "/agent-channels/webhooks/") + path


def search_numbers(*, agent, country="US", area_code=None, limit=5):
    policy_mod.require(agent, "sms")
    provider = sms_provider()
    numbers = provider.search_numbers(country=country, area_code=area_code, limit=limit)
    return {"numbers": numbers, "monthly_cents": provider.number_price_cents(country)}


def buy_number(*, agent, number=None, country="US", area_code=None, idempotency_key):
    """Buy a number for the agent (a specific one, or the first available)."""
    key, policy = policy_mod.require(agent, "sms")
    if policy.require_approval:
        raise PermissionDenied("This agent needs owner approval for purchases.")
    provider = sms_provider()
    price = provider.number_price_cents(country)
    entry, replayed = policy_mod.reserve(agent=key, kind="sms.number", amount_cents=price,
                                         description=f"Phone number ({country})",
                                         dedupe_key=f"number:{idempotency_key}")
    existing = Endpoint.objects.filter(spend=entry).first()
    if existing is not None:
        return existing
    label = f"agent-channels-{entry.pk}"
    if replayed:
        found = provider.find_number(label)  # recover a purchase whose response was lost
        if found is not None:
            return _record_number(key, entry, found)
    if number is None:
        choices = provider.search_numbers(country=country, area_code=area_code, limit=1)
        if not choices:
            policy_mod.settle(entry, charged=False)
            raise InvalidRequest(fields={"area_code": "No numbers available there."})
        number = choices[0]
    number = _number(number, "number")
    try:
        bought = provider.buy_number(number, webhook_url=_webhook_url("twilio/"), label=label)
    except ProviderError as exc:
        if exc.provider_code == "connect" or not exc.retriable:
            policy_mod.settle(entry, charged=False)
        raise  # unknown outcome stays reserved; retry with the same key looks it up
    return _record_number(key, entry, bought)


def _record_number(key, entry, bought):
    policy_mod.settle(entry, charged=True)
    endpoint, _ = Endpoint.objects.get_or_create(
        address=bought.number,
        defaults={"agent": key, "kind": Endpoint.Kind.SMS, "provider": "twilio",
                  "provider_id": bought.provider_id, "spend": entry})
    policy_mod.audit(agent=key, actor=key, verb="sms.number_bought", target=endpoint.address,
                     data={"monthly_cents": entry.amount_cents})
    return endpoint


def list_numbers(*, agent):
    return list(Endpoint.objects.filter(agent=agent, kind=Endpoint.Kind.SMS, is_active=True))


def send_sms(*, agent, from_number, to, body, idempotency_key):
    key, policy = policy_mod.require(agent, "sms")
    from_number = _number(from_number, "from_number")
    to = _number(to, "to")
    endpoint = Endpoint.objects.filter(address=from_number, kind=Endpoint.Kind.SMS, is_active=True).first()
    if endpoint is None or endpoint.agent != key:
        raise NotFound()
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_SMS:
        raise InvalidRequest(fields={"body": f"Required, at most {MAX_SMS} characters."})
    if OptOut.objects.filter(kind="sms", address=to).exists():
        raise PermissionDenied("That number replied STOP; it can't be texted.")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
        raise InvalidRequest(fields={"idempotency_key": "Required, at most 200 characters."})
    with transaction.atomic():
        try:
            with transaction.atomic():
                message = ExternalMessage.objects.create(
                    endpoint=endpoint, direction=ExternalMessage.Direction.OUT,
                    status=(ExternalMessage.Status.PENDING_APPROVAL if policy.require_approval
                            else ExternalMessage.Status.QUEUED),
                    counterparty=to, recipients=[to], body=body, dedupe_key=idempotency_key)
        except IntegrityError:
            message = ExternalMessage.objects.get(endpoint=endpoint, direction="out", dedupe_key=idempotency_key)
            if message.body != body or message.counterparty != to:
                raise InvalidRequest(fields={"idempotency_key": "Already used for a different text."}) from None
            return message  # never re-send automatically: Twilio has no send idempotency
    if message.status == ExternalMessage.Status.PENDING_APPROVAL:
        return message
    return _deliver(message)


def _deliver(message):
    endpoint = message.endpoint
    provider = sms_provider()
    segments = max(1, -(-len(message.body) // 153))
    entry, _ = policy_mod.reserve(agent=endpoint.agent, kind="sms.message",
                                  amount_cents=provider.message_price_cents() * segments,
                                  description=f"SMS to {message.counterparty[:4]}…",
                                  dedupe_key=f"sms:{message.pk}")
    ExternalMessage.objects.filter(pk=message.pk).update(attempts=message.attempts + 1)
    try:
        sent = provider.send(from_number=endpoint.address, to=message.counterparty, body=message.body,
                             status_url=_webhook_url("twilio/"))
    except ProviderError as exc:
        if exc.provider_code == "connect" or not exc.retriable:
            policy_mod.settle(entry, charged=False)
            message.status = ExternalMessage.Status.FAILED
            message.error = exc.message[:300]
            message.save(update_fields=["status", "error", "updated_at"])
        else:
            message.error = "Outcome unknown (provider timeout); not re-sent automatically."
            message.save(update_fields=["error", "updated_at"])
        raise
    policy_mod.settle(entry, charged=True)
    message.status = ExternalMessage.Status.SENT
    message.provider_message_id = sent.provider_message_id
    message.save(update_fields=["status", "provider_message_id", "updated_at"])
    policy_mod.audit(agent=endpoint.agent, actor=endpoint.agent, verb="sms.sent", target=endpoint.address,
                     data={"message": str(message.pk), "segments": segments})
    return message


def handle_keywords(from_number, body):
    """Carrier-style STOP/START. Returns True if the message was a keyword."""
    word = (body or "").strip().lower()
    if word in STOP_WORDS:
        OptOut.objects.get_or_create(kind="sms", address=from_number, defaults={"source": "sms_stop"})
        policy_mod.audit(verb="sms.opt_out", target=from_number[:4] + "…")
        return True
    if word in START_WORDS:
        OptOut.objects.filter(kind="sms", address=from_number).delete()
        policy_mod.audit(verb="sms.opt_in", target=from_number[:4] + "…")
        return True
    return False
