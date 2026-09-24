"""AgentMail email provider (https://docs.agentmail.to).

Endpoints used (base ``https://api.agentmail.to/v0``):
``POST /domains`` (returns DNS ``records``, idempotent via ``client_id``),
``GET /domains/{id}``, ``POST /domains/{id}/verify``, ``POST /inboxes``
(``client_id`` idempotent), ``POST /inboxes/{inbox}/messages/send`` and
``POST /inboxes/{inbox}/messages/{message}/reply`` (``Idempotency-Key`` header,
24h, same key + different body -> 409).

Webhooks are delivered by Svix: headers ``svix-id``, ``svix-timestamp``,
``svix-signature`` (``v1,<base64 HMAC-SHA256>`` of ``id.timestamp.body`` keyed
by the base64 part of the ``whsec_`` secret). Verified here without the svix
package so the dependency set stays small.
"""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote

import httpx

from ..errors import PermissionDenied
from . import (
    DnsRecord,
    EmailDomain,
    InboundMessage,
    Inbox,
    SentMessage,
    StatusUpdate,
    config,
    require,
)
from .http import TIMEOUT, request

PROVIDER = "AgentMail"
TOLERANCE_SECONDS = 300
RECEIVED = {"message.received"}  # spam/blocked/unauthenticated variants are ignored
STATUS_EVENTS = {"message.sent": "sent", "message.delivered": "delivered",
                 "message.bounced": "bounced", "message.rejected": "failed",
                 "message.complained": "delivered"}


def _q(value):
    return quote(str(value), safe="")


def verify_svix(secret, body, headers, *, now=None):
    """Raise ``PermissionDenied`` unless the Svix signature is valid and fresh."""
    headers = {k.lower(): v for k, v in headers.items()}
    msg_id, stamp, signatures = (headers.get("svix-id"), headers.get("svix-timestamp"),
                                 headers.get("svix-signature"))
    if not (secret and msg_id and stamp and signatures):
        raise PermissionDenied()
    try:
        age = abs((now or time.time()) - int(stamp))
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except (ValueError, TypeError):
        raise PermissionDenied() from None
    if age > TOLERANCE_SECONDS:
        raise PermissionDenied()
    signed = f"{msg_id}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return msg_id
    raise PermissionDenied()


def _domain(body):
    records = tuple(
        DnsRecord(type=str(r.get("type", "")).upper(), name=str(r.get("name", "")),
                  value=str(r.get("value", "")), priority=r.get("priority"))
        for r in body.get("records", []) if isinstance(r, dict)
    )
    return EmailDomain(provider_id=str(body.get("domain_id") or body.get("domain")),
                       status=str(body.get("status", "")).upper(), records=records)


def _addresses(value):
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value or []]


class AgentMailProvider:
    def __init__(self, *, api_key=None, webhook_secret=None, base_url=None, transport=None):
        self.webhook_secret = webhook_secret or config("AGENTMAIL_WEBHOOK_SECRET")
        self.client = httpx.Client(
            base_url=base_url or config("AGENTMAIL_BASE_URL", "https://api.agentmail.to/v0/"),
            timeout=TIMEOUT, transport=transport,
            headers={"Authorization": f"Bearer {api_key or require('AGENTMAIL_API_KEY')}"},
        )

    def _call(self, method, path, **kwargs):
        response = request(self.client, method, path, provider=PROVIDER, **kwargs)
        return response.json() if response.content else {}

    # -- domains
    def add_domain(self, name, *, client_id):
        return _domain(self._call("POST", "domains", json={"domain": name, "client_id": client_id,
                                                            "feedback_enabled": True}))

    def get_domain(self, provider_id):
        return _domain(self._call("GET", f"domains/{_q(provider_id)}"))

    def verify_domain(self, provider_id):
        self._call("POST", f"domains/{_q(provider_id)}/verify")

    # -- inboxes and mail
    def create_inbox(self, *, username, domain, display_name, client_id):
        payload = {"username": username, "display_name": display_name, "client_id": client_id}
        if domain:
            payload["domain"] = domain
        body = self._call("POST", "inboxes", json=payload)
        return Inbox(provider_id=str(body.get("inbox_id")), address=str(body.get("email")).lower())

    def send(self, *, inbox_id, to, subject, text, cc, bcc, idempotency_key):
        payload = {"to": to, "subject": subject, "text": text}
        if cc:
            payload["cc"] = cc
        if bcc:
            payload["bcc"] = bcc
        body = self._call("POST", f"inboxes/{_q(inbox_id)}/messages/send", json=payload,
                          headers={"Idempotency-Key": idempotency_key})
        return SentMessage(str(body.get("message_id", "")), str(body.get("thread_id", "")))

    def reply(self, *, inbox_id, message_id, text, idempotency_key):
        body = self._call("POST", f"inboxes/{_q(inbox_id)}/messages/{_q(message_id)}/reply",
                          json={"text": text}, headers={"Idempotency-Key": idempotency_key})
        return SentMessage(str(body.get("message_id", "")), str(body.get("thread_id", "")))

    # -- webhooks
    def parse_webhook(self, body, headers):
        """Verify, then normalise. Returns InboundMessage/StatusUpdate items (possibly none)."""
        event_id = verify_svix(self.webhook_secret, body, headers)
        try:
            payload = json.loads(body)
        except ValueError:
            return []
        kind = payload.get("event_type")
        event_id = str(payload.get("event_id") or event_id)
        if kind in RECEIVED:
            message = payload.get("message") or {}
            return [InboundMessage(
                event_id=event_id,
                to_address=str(message.get("inbox_id", "")).lower(),
                from_address=str(message.get("from", ""))[:320],
                subject=str(message.get("subject", ""))[:998],
                body=str(message.get("extracted_text") or message.get("text") or ""),
                provider_message_id=str(message.get("message_id", "")),
                thread_ref=str(message.get("thread_id", "")),
                in_reply_to=str(message.get("in_reply_to", "") or ""),
                extra={"to": _addresses(message.get("to"))},
            )]
        if kind in STATUS_EVENTS:
            section = next((payload.get(k) for k in ("send", "delivery", "bounce", "reject", "complaint")
                            if isinstance(payload.get(k), dict)), {})
            return [StatusUpdate(event_id=event_id,
                                 provider_message_id=str(section.get("message_id", "")),
                                 status=STATUS_EVENTS[kind], detail=kind)]
        return []
