"""Autonomous domain flow: search -> check -> buy -> DNS -> email domain -> verify.

Each step is resumable: ``advance_domain`` moves a domain forward from whatever
state it is in, so a timed-out or crashed run just calls it again. The purchase
is reserved against the agent's spending limit before the registrar is called
and is never re-POSTed for the same domain (the registrar is polled instead).
"""

import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import policy as policy_mod
from .errors import Conflict, InvalidRequest, NotFound, PermissionDenied, ProviderError
from .models import Domain
from .providers import DnsRecord, dns_provider, email_provider, registrar_provider

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _clean(name):
    name = (name or "").strip().lower().rstrip(".")
    if not DOMAIN_RE.fullmatch(name):
        raise InvalidRequest(fields={"domain": "A domain name like example.com."})
    return name


def _allowed(policy, quote):
    tld = quote.name.rsplit(".", 1)[-1]
    if policy.allowed_tlds and tld not in policy.allowed_tlds:
        return f".{tld} is not in this agent's allowed endings."
    if policy.max_domain_price_cents is not None and quote.price_cents > policy.max_domain_price_cents:
        return "Price is above this agent's per-domain maximum."
    return ""


def search_domains(*, agent, query, limit=10):
    _, policy = policy_mod.require(agent, "domains")
    query = (query or "").strip()
    if not query or len(query) > 100:
        raise InvalidRequest(fields={"query": "Required, at most 100 characters."})
    results = []
    for quote in registrar_provider().search(query, limit):
        note = _allowed(policy, quote) if quote.registrable else quote.reason
        results.append({"domain": quote.name, "available": quote.registrable and not note,
                        "price_cents": quote.price_cents, "renewal_cents": quote.renewal_cents,
                        "currency": quote.currency, "note": note})
    return results


def buy_domain(*, agent, domain, idempotency_key):
    """Buy a domain for the agent and set it up for email. Returns the Domain row.

    Always re-checks live price/availability first. Retrying with the same key
    returns the same purchase; it never buys twice.
    """
    key, policy = policy_mod.require(agent, "domains")
    name = _clean(domain)
    existing = Domain.objects.filter(name=name).first()
    if existing is not None:
        if existing.owner_agent != key:
            raise Conflict("That domain is already managed here.")
        return advance_domain(domain=existing)
    registrar = registrar_provider()
    quotes = registrar.check([name])
    quote = next((q for q in quotes if q.name == name), None)
    if quote is None or not quote.registrable:
        raise InvalidRequest(fields={"domain": f"Not available ({quote.reason if quote else 'unknown'})."})
    if quote.currency != "USD":
        raise InvalidRequest(fields={"domain": "Only USD-priced domains are supported."})
    note = _allowed(policy, quote)
    if note:
        raise PermissionDenied(note)
    if policy.require_approval:
        raise PermissionDenied("This agent needs owner approval for purchases.")
    entry, replayed = policy_mod.reserve(
        agent=key, kind="domain.register", amount_cents=quote.price_cents,
        description=f"Register {name}", dedupe_key=f"domain:{idempotency_key}")
    if replayed:
        row = Domain.objects.filter(spend=entry).first()
        if row is None:
            raise Conflict("That idempotency key was used for a different purchase.")
        return advance_domain(domain=row)
    try:
        with transaction.atomic():
            row = Domain.objects.create(name=name, owner_agent=key, purchased=True,
                                        status=Domain.Status.REGISTERING, price_cents=quote.price_cents,
                                        renewal_cents=quote.renewal_cents, spend=entry)
    except IntegrityError:
        policy_mod.settle(entry, charged=False)
        raise Conflict("That domain is already being registered.") from None
    policy_mod.audit(agent=key, actor=key, verb="domain.purchase_started", target=name,
                     data={"price_cents": quote.price_cents, "renewal_cents": quote.renewal_cents})
    try:
        registration = registrar.register(name, auto_renew=policy.auto_renew)
    except ProviderError as exc:
        if exc.provider_code == "connect" or (not exc.retriable and exc.provider_code != "timeout"):
            # Definitely not registered: release the money and drop the row.
            policy_mod.settle(entry, charged=False)
            _fail(row, "Registrar refused the purchase.")
            raise
        # Unknown outcome (timeout/5xx): keep reserved; advance_domain polls status.
        row.detail = "Registration outcome pending; will check status."
        row.save(update_fields=["detail", "updated_at"])
        return row
    _apply_registration(row, registration)
    return advance_domain(domain=row)


def _fail(row, detail):
    row.status = Domain.Status.FAILED
    row.detail = detail[:300]
    row.save(update_fields=["status", "detail", "updated_at"])
    policy_mod.audit(agent=row.owner_agent, verb="domain.failed", target=row.name, data={"detail": detail})


def _apply_registration(row, registration):
    if registration.state == "succeeded":
        policy_mod.settle(row.spend, charged=True)
        row.status = Domain.Status.DNS
        row.detail = ""
        policy_mod.audit(agent=row.owner_agent, verb="domain.registered", target=row.name,
                         data={"price_cents": row.price_cents})
    elif registration.state == "failed":
        policy_mod.settle(row.spend, charged=False)
        row.status = Domain.Status.FAILED
        row.detail = f"Registration failed ({registration.detail or 'no reason given'})."
    elif registration.state in ("action_required", "blocked"):
        row.status = Domain.Status.ACTION_REQUIRED
        row.detail = "Registrar needs account action (check the Cloudflare dashboard)."
    row.save(update_fields=["status", "detail", "updated_at"])


def add_existing_domain(*, user, domain, owner_agent=""):
    """Host/owner connects a domain already in the Cloudflare account (no purchase)."""
    if user is None or not getattr(user, "is_superuser", False):
        if not (owner_agent and policy_mod.identity.can_manage(user, owner_agent)):
            raise PermissionDenied()
    name = _clean(domain)
    row, _ = Domain.objects.get_or_create(
        name=name, defaults={"owner_agent": owner_agent, "status": Domain.Status.DNS})
    policy_mod.audit(agent=owner_agent, actor=f"user:{user.pk}", verb="domain.connected", target=name)
    return advance_domain(domain=row)


def advance_domain(*, domain):
    """Move a domain through the remaining setup steps. Safe to call repeatedly."""
    row = domain if isinstance(domain, Domain) else Domain.objects.filter(pk=domain).first()
    if row is None:
        raise NotFound()
    if row.status == Domain.Status.REGISTERING:
        _apply_registration(row, registrar_provider().status(row.name))
    if row.status == Domain.Status.DNS:
        email = email_provider()
        email_domain = email.add_domain(row.name, client_id=f"domain-{row.pk}")
        dns = dns_provider()
        zone_id = row.zone_id or dns.ensure_zone(row.name)
        dns.upsert_records(zone_id, [_fqdn(r, row.name) for r in email_domain.records])
        row.zone_id = zone_id
        row.email_domain_id = email_domain.provider_id
        row.status = Domain.Status.VERIFYING
        row.save(update_fields=["zone_id", "email_domain_id", "status", "updated_at"])
        policy_mod.audit(agent=row.owner_agent, verb="domain.dns_configured", target=row.name,
                         data={"records": len(email_domain.records)})
        try:
            email.verify_domain(row.email_domain_id)
        except ProviderError:
            pass  # verification is re-checked on the next advance / domain.verified webhook
    if row.status == Domain.Status.VERIFYING:
        state = email_provider().get_domain(row.email_domain_id)
        if state.status == "VERIFIED":
            mark_verified(row)
        elif state.status in ("FAILED", "INVALID"):
            row.detail = "Email records not valid yet; will retry."
            row.save(update_fields=["detail", "updated_at"])
    return row


def mark_verified(row):
    if row.status == Domain.Status.READY:
        return row
    row.status = Domain.Status.READY
    row.detail = ""
    if row.purchased and row.warmup_started_at is None:
        row.warmup_started_at = timezone.now()
    row.save(update_fields=["status", "detail", "warmup_started_at", "updated_at"])
    policy_mod.audit(agent=row.owner_agent, verb="domain.ready", target=row.name)
    return row


def _fqdn(record, zone):
    """Provider records may be relative ('_dmarc') or absolute; Cloudflare accepts FQDNs."""
    name = record.name.rstrip(".")
    if name in ("", "@"):
        name = zone
    elif name != zone and not name.endswith("." + zone):
        name = f"{name}.{zone}"
    return DnsRecord(type=record.type, name=name, value=record.value, priority=record.priority)


def list_domains(*, agent):
    return list(Domain.objects.filter(owner_agent=agent).order_by("name"))
