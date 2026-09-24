"""Provider webhook endpoints. Signatures are verified before anything is parsed.

Mount in the host URLconf::

    path("agent-channels/webhooks/", include("django_agent_channels.urls")),

AgentMail: add a webhook in the AgentMail console pointing at
``<PUBLIC_BASE_URL>/agent-channels/webhooks/agentmail/`` and set
``AGENTMAIL_WEBHOOK_SECRET``. Twilio numbers bought here are pointed at
``.../twilio/`` automatically.
"""

import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import inbound
from .errors import ChannelError, PermissionDenied
from .providers import config, email_provider, sms_provider

logger = logging.getLogger(__name__)
MAX_BODY = 5 * 1024 * 1024


def _too_big(request):
    return len(request.body) > MAX_BODY


@csrf_exempt
@require_POST
def agentmail_webhook(request):
    if _too_big(request):
        return HttpResponseBadRequest()
    try:
        items = email_provider().parse_webhook(request.body, dict(request.headers))
    except PermissionDenied:
        return HttpResponse(status=401)
    except ChannelError:
        return HttpResponse(status=503)
    inbound.process("agentmail", items)
    try:
        payload = json.loads(request.body)
    except ValueError:
        payload = {}
    if payload.get("event_type") == "domain.verified":
        domain = payload.get("domain") or {}
        inbound.domain_verified(str(domain.get("domain_id") or domain.get("domain") or ""))
    return HttpResponse(status=204)


@csrf_exempt
@require_POST
def twilio_webhook(request):
    if _too_big(request):
        return HttpResponseBadRequest()
    # Twilio signs the exact public URL it called.
    base = (config("PUBLIC_BASE_URL") or "").rstrip("/")
    url = base + request.get_full_path()
    params = {key: request.POST.get(key) for key in request.POST}
    try:
        items = sms_provider().parse_webhook(url, params, dict(request.headers))
    except PermissionDenied:
        return HttpResponse(status=401)
    except ChannelError:
        return HttpResponse(status=503)
    inbound.process("twilio", items)
    # Empty TwiML: no automatic reply (the agent replies via send_sms).
    return HttpResponse('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        content_type="text/xml")
