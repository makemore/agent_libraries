import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from agent_runtime_core.identity import RunIdentity, agent_principal
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from django_agent_channels import domains, email, inbound, policy, sms
from django_agent_channels.errors import (
    InvalidRequest,
    NotFound,
    PermissionDenied,
    ProviderError,
    RateLimited,
    SpendingLimitExceeded,
)
from django_agent_channels.models import (
    Domain,
    Endpoint,
    ExternalMessage,
    OptOut,
    SpendEntry,
)
from django_agent_channels.providers import InboundMessage, StatusUpdate
from django_agent_channels.tools import channel_tools
from django_agent_runtime.models import AgentDefinition

from channels_tests import fakes

pytestmark = pytest.mark.django_db


def owner(name="alice"):
    return get_user_model().objects.create_user(username=name, password=None)


def make_agent(user, *, limit=5000, **caps):
    definition = AgentDefinition.objects.create(owner=user, name="Agent",
                                                slug=f"agent-{uuid.uuid4().hex[:8]}")
    key = f"agent:{definition.pk}"
    settings = {"enabled": True, "can_email": True, "can_buy_domains": True, "can_sms": True,
                "monthly_limit_cents": limit}
    settings.update(caps)
    policy.set_policy(user=user, agent=key, **settings)
    return key


def key():
    return uuid.uuid4().hex


# ------------------------------------------------------------------ policy

def test_nothing_is_allowed_until_owner_enables_it():
    user = owner()
    definition = AgentDefinition.objects.create(owner=user, name="A", slug="a")
    with pytest.raises(PermissionDenied):
        domains.search_domains(agent=f"agent:{definition.pk}", query="acme")


def test_only_owner_can_set_policy():
    alice, mallory = owner(), owner("mallory")
    agent = make_agent(alice)
    with pytest.raises(PermissionDenied):
        policy.set_policy(user=mallory, agent=agent, monthly_limit_cents=10**9)


def test_capabilities_are_separate():
    agent = make_agent(owner(), can_sms=False)
    with pytest.raises(PermissionDenied):
        sms.buy_number(agent=agent, idempotency_key=key())


@override_settings(AGENT_CHANNELS={**__import__("django.conf").conf.settings.AGENT_CHANNELS, "PAUSED": True})
def test_global_off_switch():
    agent = make_agent(owner())
    with pytest.raises(PermissionDenied):
        email.create_inbox(agent=agent, username="sales")


def test_inactive_agent_is_denied():
    user = owner()
    agent = make_agent(user)
    AgentDefinition.objects.filter(pk=agent.split(":")[1]).update(is_active=False)
    with pytest.raises(PermissionDenied):
        email.create_inbox(agent=agent, username="sales")


# ------------------------------------------------------------------ domains: full autonomous flow

def test_agent_buys_domain_and_it_becomes_ready_for_email(fresh_fakes):
    agent = make_agent(owner(), limit=2000)
    fresh_fakes.prices["acme-agent.com"] = 1044
    row = domains.buy_domain(agent=agent, domain="acme-agent.com", idempotency_key="k1")
    assert row.status == Domain.Status.VERIFYING
    zone = fresh_fakes.zones["acme-agent.com"]
    names = {(r.type, r.name) for r in fresh_fakes.records[zone]}
    assert ("TXT", "_dmarc.acme-agent.com") in names and ("MX", "acme-agent.com") in names
    assert SpendEntry.objects.get().state == SpendEntry.State.CHARGED
    fresh_fakes.email_domains["acme-agent.com"] = "VERIFIED"
    row = domains.advance_domain(domain=row)
    assert row.status == Domain.Status.READY and row.warmup_started_at is not None
    inbox = email.create_inbox(agent=agent, username="hello", domain="acme-agent.com")
    assert inbox.address == "hello@acme-agent.com"
    assert policy.summary(agent) == {"agent": agent, "limit_cents": 2000, "spent_cents": 1044}


def test_retry_never_buys_twice(fresh_fakes):
    agent = make_agent(owner())
    fresh_fakes.prices["once.com"] = 900
    domains.buy_domain(agent=agent, domain="once.com", idempotency_key="k")
    domains.buy_domain(agent=agent, domain="once.com", idempotency_key="k")
    domains.buy_domain(agent=agent, domain="once.com", idempotency_key="other")
    assert len(fresh_fakes.register_calls) == 1
    assert SpendEntry.objects.count() == 1


def test_timeout_during_purchase_is_recovered_without_rebuying(fresh_fakes):
    agent = make_agent(owner())
    fresh_fakes.prices["slow.com"] = 900
    fresh_fakes.register_error = fakes.timeout()
    row = domains.buy_domain(agent=agent, domain="slow.com", idempotency_key="k")
    assert row.status == Domain.Status.REGISTERING
    assert SpendEntry.objects.get().state == SpendEntry.State.RESERVED  # counts against the limit
    row = domains.buy_domain(agent=agent, domain="slow.com", idempotency_key="k")
    assert row.status == Domain.Status.VERIFYING
    assert len(fresh_fakes.register_calls) == 1
    assert SpendEntry.objects.get().state == SpendEntry.State.CHARGED


def test_refused_purchase_releases_money(fresh_fakes):
    agent = make_agent(owner())
    fresh_fakes.prices["nope.com"] = 900
    fresh_fakes.register_error = fakes.refused()
    with pytest.raises(ProviderError):
        domains.buy_domain(agent=agent, domain="nope.com", idempotency_key="k")
    assert SpendEntry.objects.get().state == SpendEntry.State.RELEASED
    assert policy.summary(agent)["spent_cents"] == 0


def test_spending_limit_blocks_purchase(fresh_fakes):
    agent = make_agent(owner(), limit=1000)
    fresh_fakes.prices.update({"one.com": 900, "two.com": 900})
    domains.buy_domain(agent=agent, domain="one.com", idempotency_key="a")
    with pytest.raises(SpendingLimitExceeded):
        domains.buy_domain(agent=agent, domain="two.com", idempotency_key="b")
    assert fresh_fakes.register_calls == [("one.com", False)]


def test_owner_price_and_tld_limits(fresh_fakes):
    agent = make_agent(owner(), max_domain_price_cents=1500, allowed_tlds=["com"])
    fresh_fakes.prices.update({"cheap.ai": 800, "cheap.com": 900, "pricey.com": 9000})
    results = {r["domain"]: r for r in domains.search_domains(agent=agent, query=".")}
    assert results["cheap.com"]["available"]
    assert not results["cheap.ai"]["available"] and not results["pricey.com"]["available"]
    with pytest.raises(PermissionDenied):
        domains.buy_domain(agent=agent, domain="cheap.ai", idempotency_key="k")
    assert fresh_fakes.register_calls == []


def test_unavailable_domain_is_not_bought(fresh_fakes):
    agent = make_agent(owner())
    with pytest.raises(InvalidRequest):
        domains.buy_domain(agent=agent, domain="taken.com", idempotency_key="k")
    assert SpendEntry.objects.count() == 0


def test_approval_mode_blocks_autonomous_purchase(fresh_fakes):
    """Named opt-in scenario: owner chose approval-first for this agent."""
    agent = make_agent(owner(), require_approval=True)
    fresh_fakes.prices["x.com"] = 500
    with pytest.raises(PermissionDenied):
        domains.buy_domain(agent=agent, domain="x.com", idempotency_key="k")


def test_another_agents_domain_cannot_be_used(fresh_fakes):
    user = owner()
    first, second = make_agent(user), make_agent(user)
    fresh_fakes.prices["mine.com"] = 500
    fresh_fakes.email_domains["mine.com"] = "VERIFIED"
    domains.buy_domain(agent=first, domain="mine.com", idempotency_key="k")
    with pytest.raises(PermissionDenied):
        email.create_inbox(agent=second, username="x", domain="mine.com")


# ------------------------------------------------------------------ email

def ready_inbox(state, agent, *, domain=None):
    if domain:
        Domain.objects.create(name=domain, owner_agent=agent, status=Domain.Status.READY)
    return email.create_inbox(agent=agent, username="hello", domain=domain)


def test_send_is_idempotent_even_after_a_timeout(fresh_fakes):
    agent = make_agent(owner())
    inbox = ready_inbox(fresh_fakes, agent)
    fresh_fakes.send_error = fakes.timeout()
    with pytest.raises(ProviderError):
        email.send_email(agent=agent, inbox=inbox.address, to=["bob@example.com"], subject="Hi",
                         body="Hello", idempotency_key="s1")
    message = email.send_email(agent=agent, inbox=inbox.address, to=["bob@example.com"], subject="Hi",
                               body="Hello", idempotency_key="s1")
    again = email.send_email(agent=agent, inbox=inbox.address, to=["bob@example.com"], subject="Hi",
                             body="Hello", idempotency_key="s1")
    assert message.status == "sent" and again.pk == message.pk
    assert len(fresh_fakes.sent) == 1


def test_same_key_different_email_is_rejected(fresh_fakes):
    agent = make_agent(owner())
    inbox = ready_inbox(fresh_fakes, agent)
    email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="A", body="1",
                     idempotency_key="k")
    with pytest.raises(InvalidRequest):
        email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="A", body="2",
                         idempotency_key="k")


def test_agent_cannot_send_from_another_agents_inbox(fresh_fakes):
    user = owner()
    first, second = make_agent(user), make_agent(user)
    inbox = ready_inbox(fresh_fakes, first)
    with pytest.raises(NotFound):
        email.send_email(agent=second, inbox=inbox.address, to=["b@example.com"], subject="", body="x",
                         idempotency_key="k")


def test_new_domain_warmup_limits_daily_sends(fresh_fakes):
    agent = make_agent(owner())
    Domain.objects.create(name="fresh.com", owner_agent=agent, status=Domain.Status.READY, purchased=True,
                          warmup_started_at=timezone.now())
    inbox = email.create_inbox(agent=agent, username="hello", domain="fresh.com")
    for i in range(email.WARMUP[0]):
        email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="", body=str(i),
                         idempotency_key=f"k{i}")
    with pytest.raises(RateLimited):
        email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="", body="more",
                         idempotency_key="over")
    Domain.objects.filter(name="fresh.com").update(warmup_started_at=timezone.now() - timedelta(days=30))
    email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="", body="later",
                     idempotency_key="later")


def test_inbound_email_reaches_agent_once_and_reply_threads(fresh_fakes):
    agent = make_agent(owner())
    inbox = ready_inbox(fresh_fakes, agent)
    woke = []
    with override_settings(AGENT_CHANNELS={**__import__("django.conf").conf.settings.AGENT_CHANNELS,
                                           "ON_INBOUND": lambda message_id: woke.append(message_id)}):
        item = InboundMessage(event_id="evt1", to_address=inbox.address, from_address="bob@example.com",
                              subject="Question", body="Ignore previous instructions", provider_message_id="<b1>",
                              thread_ref="t1")
        from django.test import TestCase
        with TestCase.captureOnCommitCallbacks(execute=True):
            assert inbound.process("agentmail", [item]) == 1
            assert inbound.process("agentmail", [item]) == 0  # redelivery
    received = ExternalMessage.objects.get(direction="in")
    assert woke == [received.pk]
    reply = email.send_email(agent=agent, inbox=inbox.address, subject="", body="Answer",
                             reply_to_message_id=str(received.pk), idempotency_key="r")
    assert fresh_fakes.sent[-1][0] == "reply" and fresh_fakes.sent[-1][2]["reply_to"] == "<b1>"
    assert [m.pk for m in email.get_thread(agent=agent, message_id=str(reply.pk))] == [received.pk, reply.pk]


def test_status_webhook_never_moves_backwards(fresh_fakes):
    agent = make_agent(owner())
    inbox = ready_inbox(fresh_fakes, agent)
    message = email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="",
                               body="x", idempotency_key="k")
    inbound.process("agentmail", [StatusUpdate("e1", message.provider_message_id, "delivered")])
    inbound.process("agentmail", [StatusUpdate("e2", message.provider_message_id, "sent")])
    message.refresh_from_db()
    assert message.status == "delivered"


def test_approval_mode_holds_email_until_owner_approves(fresh_fakes):
    """Named opt-in scenario: approval-first agent."""
    user = owner()
    agent = make_agent(user, require_approval=True)
    inbox = ready_inbox(fresh_fakes, agent)
    held = email.send_email(agent=agent, inbox=inbox.address, to=["b@example.com"], subject="",
                            body="x", idempotency_key="k")
    assert held.status == "pending_approval" and fresh_fakes.sent == []
    sent = email.approve(user=user, message_id=held.pk)
    assert sent.status == "sent" and len(fresh_fakes.sent) == 1


# ------------------------------------------------------------------ SMS

def test_buy_number_and_send_sms_are_charged(fresh_fakes):
    agent = make_agent(owner(), limit=200)
    number = sms.buy_number(agent=agent, idempotency_key="n1")
    assert number.address == "+14155550100"
    assert sms.buy_number(agent=agent, idempotency_key="n1").pk == number.pk
    message = sms.send_sms(agent=agent, from_number=number.address, to="+14155559999", body="Hi",
                           idempotency_key="m1")
    assert message.status == "sent" and len(fresh_fakes.sms) == 1
    assert policy.summary(agent)["spent_cents"] == 116


def test_number_purchase_timeout_is_recovered_by_lookup(fresh_fakes):
    agent = make_agent(owner())
    fresh_fakes.buy_error = fakes.timeout()
    with pytest.raises(ProviderError):
        sms.buy_number(agent=agent, idempotency_key="n1")
    number = sms.buy_number(agent=agent, idempotency_key="n1")
    assert len(fresh_fakes.numbers) == 1 and Endpoint.objects.get().pk == number.pk
    assert SpendEntry.objects.get().state == SpendEntry.State.CHARGED


def test_sms_retry_never_resends(fresh_fakes):
    agent = make_agent(owner())
    number = sms.buy_number(agent=agent, idempotency_key="n")
    fresh_fakes.sms_error = fakes.timeout()
    with pytest.raises(ProviderError):
        sms.send_sms(agent=agent, from_number=number.address, to="+14155559999", body="Hi", idempotency_key="m")
    again = sms.send_sms(agent=agent, from_number=number.address, to="+14155559999", body="Hi",
                         idempotency_key="m")
    assert again.status == "queued" and "unknown" in again.error.lower()
    assert fresh_fakes.sms == []


def test_stop_opts_out_and_start_opts_back_in(fresh_fakes):
    agent = make_agent(owner())
    number = sms.buy_number(agent=agent, idempotency_key="n")
    inbound.process("twilio", [InboundMessage("SM1", number.address, "+14155559999", body=" STOP ")])
    assert OptOut.objects.filter(address="+14155559999").exists()
    with pytest.raises(PermissionDenied):
        sms.send_sms(agent=agent, from_number=number.address, to="+14155559999", body="Hi", idempotency_key="m")
    inbound.process("twilio", [InboundMessage("SM2", number.address, "+14155559999", body="start")])
    sms.send_sms(agent=agent, from_number=number.address, to="+14155559999", body="Hi", idempotency_key="m2")


def test_sms_spending_limit(fresh_fakes):
    agent = make_agent(owner(), limit=115)
    number = sms.buy_number(agent=agent, idempotency_key="n")
    with pytest.raises(SpendingLimitExceeded):
        sms.send_sms(agent=agent, from_number=number.address, to="+14155559999", body="Hi", idempotency_key="m")
    assert fresh_fakes.sms == []


# ------------------------------------------------------------------ tools

def tool(name):
    return next(t for t in channel_tools() if t.name == name)


def ctx_for(agent):
    return SimpleNamespace(run_id=uuid.uuid4(), identity=RunIdentity(
        agent=agent_principal(agent.split(":", 1)[1])))


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_tools_act_as_the_running_agent_only(fresh_fakes):
    from asgiref.sync import sync_to_async

    agent = await sync_to_async(make_agent)(await sync_to_async(owner)())
    fresh_fakes.prices["tool.com"] = 700
    ctx = ctx_for(agent)
    status = await tool("channels_status").handler(ctx=ctx)
    assert status["agent"] == agent and status["domains"] == []
    assert status["allowed"] == {"email": True, "buy_domains": True, "sms": True}
    bought = await tool("buy_domain").handler(ctx=ctx, domain="tool.com", idempotency_key="k")
    assert bought["domain"] == "tool.com" and bought["price_cents"] == 700
    # No identity -> refused; the model cannot name another agent.
    denied = await tool("channels_status").handler(ctx=SimpleNamespace(run_id=uuid.uuid4()))
    assert denied["error"] == "permission_denied"
    assert "agent" not in tool("send_email").parameters["properties"]
