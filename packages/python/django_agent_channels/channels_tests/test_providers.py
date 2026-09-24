"""Real provider clients against mocked HTTP (httpx.MockTransport). No network, no keys."""

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest
from django.test import override_settings
from django_agent_channels.errors import PermissionDenied, ProviderError
from django_agent_channels.providers import DnsRecord, InboundMessage, StatusUpdate
from django_agent_channels.providers.agentmail import AgentMailProvider, verify_svix
from django_agent_channels.providers.cloudflare import (
    CloudflareDNS,
    CloudflareRegistrar,
)
from django_agent_channels.providers.twilio import TwilioSMS, signature, verify_twilio

SECRET = "whsec_" + base64.b64encode(b"test-signing-key").decode()


def transport(handler, calls):
    def wrapped(request):
        calls.append(request)
        return handler(request)
    return httpx.MockTransport(wrapped)


def ok(result):
    return httpx.Response(200, json={"success": True, "errors": [], "messages": [], "result": result})


# ------------------------------------------------------------------ Cloudflare

def test_cloudflare_check_parses_prices_and_register_is_async():
    calls = []

    def handler(request):
        if request.url.path.endswith("/domain-check"):
            return ok({"domains": [{"name": "acme.dev", "registrable": True,
                                    "pricing": {"currency": "USD", "registration_cost": "10.11",
                                                "renewal_cost": "10.11"}}]})
        return httpx.Response(202, json={"success": True, "result": {"domain_name": "acme.dev",
                                                                     "state": "in_progress"}})

    registrar = CloudflareRegistrar(token="t", account_id="acc", transport=transport(handler, calls))
    [quote] = registrar.check(["acme.dev"])
    assert (quote.name, quote.registrable, quote.price_cents) == ("acme.dev", True, 1011)
    registration = registrar.register("acme.dev", auto_renew=False)
    assert registration.state == "in_progress"
    post = calls[-1]
    assert post.url.path == "/client/v4/accounts/acc/registrar/registrations"
    assert post.headers["Prefer"] == "respond-async" and post.headers["Authorization"] == "Bearer t"
    assert json.loads(post.content) == {"domain_name": "acme.dev", "auto_renew": False}


def test_cloudflare_unregistrable_reason_and_http_errors_are_safe():
    calls = []
    registrar = CloudflareRegistrar(token="t", account_id="acc", transport=transport(
        lambda r: ok({"domains": [{"name": "x.uk", "registrable": False,
                                   "reason": "extension_not_supported_via_api"}]}), calls))
    [quote] = registrar.check(["x.uk"])
    assert not quote.registrable and quote.reason == "extension_not_supported_via_api"
    failing = CloudflareRegistrar(token="secret-token", account_id="acc", transport=transport(
        lambda r: httpx.Response(500, text="internal secret-token"), []))
    with pytest.raises(ProviderError) as caught:
        failing.check(["x.com"])
    assert caught.value.retriable and "secret-token" not in str(caught.value)


def test_cloudflare_dns_upsert_is_idempotent():
    records, calls = [], []

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/dns_records"):
            name, kind = request.url.params["name"], request.url.params["type"]
            found = [r for r in records if r["name"] == name and r["type"] == kind]
            return ok(found)
        if request.method == "POST":
            body = json.loads(request.content)
            records.append({**body, "id": f"r{len(records)}"})
            return ok(records[-1])
        if request.method == "PUT":
            body = json.loads(request.content)
            rid = request.url.path.rsplit("/", 1)[1]
            for r in records:
                if r["id"] == rid:
                    r.update(body)
            return ok(body)
        return ok([])

    dns = CloudflareDNS(token="t", account_id="acc", transport=transport(handler, calls))
    wanted = [DnsRecord("TXT", "_dmarc.acme.dev", "v=DMARC1; p=reject"),
              DnsRecord("MX", "acme.dev", "inbound.agentmail.to", 10)]
    dns.upsert_records("z", wanted)
    dns.upsert_records("z", wanted)
    assert len(records) == 2 and records[1]["priority"] == 10
    dns.upsert_records("z", [DnsRecord("TXT", "_dmarc.acme.dev", "v=DMARC1; p=quarantine")])
    assert len(records) == 2 and records[0]["content"] == "v=DMARC1; p=quarantine"


# ------------------------------------------------------------------ AgentMail

def sign(body, *, secret=SECRET, msg_id="msg_1", stamp=None):
    stamp = str(int(stamp if stamp is not None else time.time()))
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest())
    return {"svix-id": msg_id, "svix-timestamp": stamp, "svix-signature": f"v1,{sig.decode()}"}


def test_svix_signature_verification():
    body = b'{"event_type":"message.received"}'
    assert verify_svix(SECRET, body, sign(body)) == "msg_1"
    with pytest.raises(PermissionDenied):
        verify_svix(SECRET, body + b" ", sign(body))
    with pytest.raises(PermissionDenied):
        verify_svix(SECRET, body, sign(body, stamp=time.time() - 3600))
    with pytest.raises(PermissionDenied):
        verify_svix(SECRET, body, {})


def test_agentmail_send_uses_idempotency_header_and_parses_webhooks():
    calls = []

    def handler(request):
        if request.url.path.endswith("/messages/send"):
            return httpx.Response(200, json={"message_id": "<m1>", "thread_id": "t1"})
        if request.url.path.endswith("/domains"):
            return httpx.Response(200, json={"domain_id": "acme.dev", "domain": "acme.dev", "status": "PENDING",
                                             "records": [{"type": "MX", "name": "acme.dev", "value": "in.x",
                                                          "priority": 10, "status": "MISSING"}]})
        return httpx.Response(404)

    provider = AgentMailProvider(api_key="k", webhook_secret=SECRET, transport=transport(handler, calls))
    sent = provider.send(inbox_id="hi@acme.dev", to=["b@example.com"], subject="S", text="T", cc=[], bcc=[],
                         idempotency_key="msg-1")
    assert sent.provider_message_id == "<m1>"
    assert calls[-1].url.raw_path.decode().startswith("/v0/inboxes/hi%40acme.dev/messages/send")
    assert calls[-1].headers["Idempotency-Key"] == "msg-1"
    domain = provider.add_domain("acme.dev", client_id="domain-1")
    assert domain.records[0] == DnsRecord("MX", "acme.dev", "in.x", 10)
    body = json.dumps({"type": "event", "event_type": "message.received", "event_id": "evt_1",
                       "message": {"inbox_id": "hi@acme.dev", "from": "Bob <b@example.com>", "subject": "Hey",
                                   "text": "full", "extracted_text": "new part", "message_id": "<in1>",
                                   "thread_id": "t9", "to": ["hi@acme.dev"]}}).encode()
    [item] = provider.parse_webhook(body, sign(body))
    assert isinstance(item, InboundMessage) and item.body == "new part" and item.event_id == "evt_1"
    status = json.dumps({"event_type": "message.delivered", "event_id": "evt_2",
                         "delivery": {"message_id": "<m1>"}}).encode()
    assert provider.parse_webhook(status, sign(status)) == [StatusUpdate("evt_2", "<m1>", "delivered",
                                                                         "message.delivered")]
    with pytest.raises(PermissionDenied):
        provider.parse_webhook(body, {"svix-id": "x"})


# ------------------------------------------------------------------ Twilio

def reference_signature(token, uri, params):
    """Verbatim logic of twilio-python RequestValidator.compute_signature (main branch)."""
    s = uri
    for name in sorted(set(params)):
        for value in sorted({params[name]}):
            s += name + value
    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode().strip()


def test_twilio_signature_matches_reference_and_accepts_port_variants():
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert signature("12345", url, params) == reference_signature("12345", url, params)
    with_port = reference_signature("12345", "https://mycompany.com:443/myapp.php?foo=1&bar=2", params)
    verify_twilio("12345", url, params, {"X-Twilio-Signature": with_port})
    with pytest.raises(PermissionDenied):
        verify_twilio("12345", url, {**params, "Digits": "9"}, {"X-Twilio-Signature": with_port})


def test_twilio_send_and_webhooks():
    calls = []

    def handler(request):
        if request.url.path.endswith("/Messages.json"):
            return httpx.Response(201, json={"sid": "SM1"})
        return httpx.Response(404)

    sms = TwilioSMS(account_sid="AC1", auth_token="tok", transport=transport(handler, calls))
    sent = sms.send(from_number="+14155550100", to="+14155559999", body="Hi", status_url="https://s/t/")
    assert sent.provider_message_id == "SM1"
    form = dict(httpx.QueryParams(calls[-1].content.decode()))
    assert form == {"To": "+14155559999", "Body": "Hi", "StatusCallback": "https://s/t/", "From": "+14155550100"}
    assert calls[-1].headers["Authorization"].startswith("Basic ")
    url = "https://studio.test/agent-channels/webhooks/twilio/"
    params = {"MessageSid": "SM9", "From": "+14155559999", "To": "+14155550100", "Body": "hello"}
    [item] = sms.parse_webhook(url, params, {"X-Twilio-Signature": signature("tok", url, params)})
    assert isinstance(item, InboundMessage) and item.body == "hello"
    with pytest.raises(PermissionDenied):
        sms.parse_webhook(url, params, {"X-Twilio-Signature": "bad"})


@override_settings(TWILIO_MESSAGING_SERVICE_SID=None)
def test_twilio_messaging_service_is_used_when_configured(settings):
    settings.AGENT_CHANNELS = {**settings.AGENT_CHANNELS, "TWILIO_MESSAGING_SERVICE_SID": "MG1"}
    calls = []
    sms = TwilioSMS(account_sid="AC1", auth_token="tok",
                    transport=transport(lambda r: httpx.Response(201, json={"sid": "SM1"}), calls))
    sms.send(from_number="+1", to="+14155559999", body="Hi", status_url="https://s/")
    form = dict(httpx.QueryParams(calls[-1].content.decode()))
    assert form["MessagingServiceSid"] == "MG1" and "From" not in form
