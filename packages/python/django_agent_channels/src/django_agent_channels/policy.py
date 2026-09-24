"""Per-agent autonomy settings, spending limits, kill switches and audit.

Owners choose what each agent may do. When enabled, the agent acts without
per-action approval; the monthly spending limit and the off switches are the
controls. ``require_approval`` is an opt-in per-agent mode.
"""

import datetime as dt

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.module_loading import import_string

from . import identity
from .errors import InvalidRequest, NotFound, PermissionDenied, SpendingLimitExceeded
from .models import AgentChannelPolicy, ChannelEvent, SpendEntry
from .providers import config

CAPABILITIES = {"email": "can_email", "domains": "can_buy_domains", "sms": "can_sms"}
EDITABLE = {"enabled", "can_email", "can_buy_domains", "can_sms", "require_approval",
            "monthly_limit_cents", "max_domain_price_cents", "allowed_tlds", "auto_renew"}


def audit(*, agent="", actor="", verb, target="", data=None):
    ChannelEvent.objects.create(agent=agent, actor=actor, verb=verb, target=str(target)[:320],
                                data=data or {})


# --------------------------------------------------------------------------- policy

def set_policy(*, user, agent, **changes):
    """Owner (or host-approved user) sets an agent's channel autonomy and limits."""
    key = identity.agent_key(agent)
    unknown = set(changes) - EDITABLE
    if unknown:
        raise InvalidRequest(fields={sorted(unknown)[0]: "Unknown setting."})
    if not identity.can_manage(user, key):
        raise PermissionDenied()
    for name in ("monthly_limit_cents", "max_domain_price_cents"):
        value = changes.get(name)
        if value is not None and (type(value) is not int or value < 0):
            raise InvalidRequest(fields={name: "Whole cents, zero or more."})
    if "allowed_tlds" in changes:
        tlds = changes["allowed_tlds"]
        if not isinstance(tlds, list) or not all(isinstance(t, str) and t for t in tlds):
            raise InvalidRequest(fields={"allowed_tlds": "List like [\"com\", \"ai\"]."})
        changes["allowed_tlds"] = sorted({t.lower().lstrip(".") for t in tlds})
    with transaction.atomic():
        policy, _ = AgentChannelPolicy.objects.select_for_update().get_or_create(agent=key)
        for name, value in changes.items():
            setattr(policy, name, value)
        policy.updated_by = f"user:{user.pk}"
        policy.save()
        audit(agent=key, actor=policy.updated_by, verb="policy.updated",
              data={k: v for k, v in changes.items()})
    return policy


def get_policy(agent):
    return AgentChannelPolicy.objects.filter(agent=agent).first()


def globally_paused():
    return bool(config("PAUSED", False))


def require(agent, capability):
    """Active agent + global switch on + agent enabled + capability granted."""
    key = identity.require_agent(agent)
    if globally_paused():
        raise PermissionDenied("Agent channels are paused by the host.")
    policy = get_policy(key)
    if policy is None or not policy.enabled or not getattr(policy, CAPABILITIES[capability]):
        raise PermissionDenied(f"This agent is not allowed to use {capability}.")
    return key, policy


# --------------------------------------------------------------------------- spending

def month_start(now=None):
    now = now or timezone.now()
    return now.astimezone(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(agent):
    total = (SpendEntry.objects.filter(agent=agent, created_at__gte=month_start())
             .exclude(state=SpendEntry.State.RELEASED).aggregate(total=Sum("amount_cents"))["total"])
    return total or 0


def reserve(*, agent, kind, amount_cents, description, dedupe_key):
    """Reserve money before a paid provider call. Returns (entry, replayed).

    Runs under the agent's policy row lock so concurrent purchases cannot both
    fit under the limit. A repeated ``dedupe_key`` returns the original entry.
    """
    if type(amount_cents) is not int or amount_cents < 0:
        raise InvalidRequest(fields={"amount": "Invalid price."})
    with transaction.atomic():
        policy = AgentChannelPolicy.objects.select_for_update().get(agent=agent)
        existing = SpendEntry.objects.filter(agent=agent, dedupe_key=dedupe_key).first()
        if existing is not None:
            if existing.kind != kind:
                raise InvalidRequest(fields={"idempotency_key": "Already used for another action."})
            return existing, True
        spent = spent_this_month(agent)
        if spent + amount_cents > policy.monthly_limit_cents:
            audit(agent=agent, verb="spend.refused", target=kind,
                  data={"amount_cents": amount_cents, "spent_cents": spent,
                        "limit_cents": policy.monthly_limit_cents})
            raise SpendingLimitExceeded(
                f"Spending limit reached: {spent + amount_cents} of "
                f"{policy.monthly_limit_cents} cents this month.")
        hook = config("SPEND_CHECK")  # optional host-wide cap: (agent=, kind=, amount_cents=) -> True
        if hook:
            check = import_string(hook) if isinstance(hook, str) else hook
            if check(agent=agent, kind=kind, amount_cents=amount_cents) is not True:
                raise SpendingLimitExceeded("The host spending cap refused this purchase.")
        try:
            with transaction.atomic():
                entry = SpendEntry.objects.create(agent=agent, kind=kind, amount_cents=amount_cents,
                                                  description=description[:300], dedupe_key=dedupe_key)
        except IntegrityError:
            return SpendEntry.objects.get(agent=agent, dedupe_key=dedupe_key), True
        audit(agent=agent, verb="spend.reserved", target=kind,
              data={"amount_cents": amount_cents, "entry": str(entry.pk)})
    return entry, False


def settle(entry, *, charged):
    """Mark a reservation charged (provider confirmed) or released (definitely not charged)."""
    state = SpendEntry.State.CHARGED if charged else SpendEntry.State.RELEASED
    updated = SpendEntry.objects.filter(pk=entry.pk, state=SpendEntry.State.RESERVED).update(state=state)
    if updated:
        audit(agent=entry.agent, verb=f"spend.{state}", target=entry.kind,
              data={"amount_cents": entry.amount_cents, "entry": str(entry.pk)})
    entry.refresh_from_db()
    return entry


def summary(agent):
    policy = get_policy(agent)
    return {"agent": agent, "limit_cents": policy.monthly_limit_cents if policy else 0,
            "spent_cents": spent_this_month(agent)}


def get_owned(model, *, agent, pk, **filters):
    obj = model.objects.filter(pk=pk, **filters).first()
    if obj is None or getattr(obj, "agent", getattr(obj, "owner_agent", None)) != agent:
        raise NotFound()
    return obj
