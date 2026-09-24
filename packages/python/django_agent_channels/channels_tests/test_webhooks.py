"""Webhook views end to end with real signature checks (named scenario: real provider classes)."""

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django_agent_channels import policy
from django_agent_channels.models import Endpoint, ExternalMessage, OptOut
from django_agent_channels.providers.twilio import signature
from django_agent_runtime.models import AgentDefinition

from channels_tests.test_providers import SECRET, sign

pytestmark = pytest.mark.django_db


@pytest.fixture
def real_webhooks(settings):
    settings.AGENT_CHANNELS = {**settings.AGENT_CHANNELS,
                               "EMAIL_PROVIDER": "django_agent_channels.providers.agentmail.AgentMailProvider",
                               "SMS_PROVIDER": "django_agent_channels.providers.twilio.TwilioSMS",
                               "AGENTMAIL_API_KEY": "unused", "AGENTMAIL_WEBHOOK_SECRET": SECRET,
                               "TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok"}


def endpoint(kind, address):
    user = get_user_model().objects.create_user(username=uuid.uuid4().hex[:8])
    agent = AgentDefinition.objects.create(owner=user, name="A", slug=uuid.uuid4().hex[:8])
    key = f"agent:{agent.pk}"
    policy.set_policy(user=user, agent=key, enabled=True, can_email=True, can_sms=True, monthly_limit_cents=100)
    return Endpoint.objects.create(agent=key, kind=kind, address=address, provider="x", provider_id=address)


def test_agentmail_webhook_verifies_and_delivers_once(client, real_webhooks):
    endpoint("email", "hi@acme.dev")
    body = json.dumps({"event_type": "message.received", "event_id": "evt_1",
                       "message": {"inbox_id": "hi@acme.dev", "from": "b@example.com", "subject": "S",
                                   "text": "hello", "message_id": "<in1>", "thread_id": "t1"}}).encode()
    headers = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in sign(body).items()}
    url = "/agent-channels/webhooks/agentmail/"
    assert client.post(url, body, content_type="application/json", **headers).status_code == 204
    assert client.post(url, body, content_type="application/json", **headers).status_code == 204
    assert ExternalMessage.objects.filter(direction="in").count() == 1
    assert client.post(url, body, content_type="application/json").status_code == 401


def test_twilio_webhook_verifies_and_handles_stop(client, real_webhooks):
    endpoint("sms", "+14155550100")
    url = "https://studio.test/agent-channels/webhooks/twilio/"
    params = {"MessageSid": "SM1", "From": "+14155559999", "To": "+14155550100", "Body": "STOP"}
    response = client.post("/agent-channels/webhooks/twilio/", params,
                           HTTP_X_TWILIO_SIGNATURE=signature("tok", url, params))
    assert response.status_code == 200 and b"<Response></Response>" in response.content
    assert OptOut.objects.filter(address="+14155559999").exists()
    forged = client.post("/agent-channels/webhooks/twilio/", {**params, "Body": "START"},
                         HTTP_X_TWILIO_SIGNATURE=signature("tok", url, params))
    assert forged.status_code == 401 and OptOut.objects.filter(address="+14155559999").exists()
