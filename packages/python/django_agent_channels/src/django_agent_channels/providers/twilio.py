"""Twilio SMS provider (REST API 2010-04-01, form-encoded, HTTP basic auth).

Endpoints: ``GET  Accounts/{sid}/AvailablePhoneNumbers/{CC}/Local.json``,
``POST Accounts/{sid}/IncomingPhoneNumbers.json`` (buy; ``SmsUrl`` webhook),
``POST Accounts/{sid}/Messages.json`` (send; ``StatusCallback``).

Inbound and status webhooks are signed with ``X-Twilio-Signature``: base64
HMAC-SHA1 (auth token) of the full URL followed by each POST parameter name and
value, sorted by name. The URL must be exactly what Twilio called, so hosts set
``PUBLIC_BASE_URL`` to the externally visible origin.

US sending needs A2P 10DLC registration (brand + campaign) on the Twilio account;
a Messaging Service SID can be configured to send through a registered campaign.
Prices are host-configured estimates used for spending limits; Twilio bills the
account directly.
"""

import base64
import hashlib
import hmac
from urllib.parse import urlparse

import httpx

from ..errors import InvalidRequest, PermissionDenied
from . import InboundMessage, PhoneNumber, SentMessage, StatusUpdate, config, require
from .http import TIMEOUT, request

PROVIDER = "Twilio"
STATUS = {"sent": "sent", "delivered": "delivered", "failed": "failed",
          "undelivered": "failed"}


def signature(auth_token, url, params):
    """Same algorithm as twilio-python's RequestValidator.compute_signature."""
    data = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def _url_variants(url):
    # Twilio may sign with or without the default port; its validator accepts both.
    parsed = urlparse(url)
    host = parsed.netloc.split(":")[0]
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return {parsed._replace(netloc=host).geturl(), parsed._replace(netloc=f"{host}:{port}").geturl()}


def verify_twilio(auth_token, url, params, headers):
    headers = {k.lower(): v for k, v in headers.items()}
    provided = headers.get("x-twilio-signature", "")
    if not (auth_token and provided and any(
            hmac.compare_digest(provided, signature(auth_token, candidate, params))
            for candidate in _url_variants(url))):
        raise PermissionDenied()


class TwilioSMS:
    def __init__(self, *, account_sid=None, auth_token=None, transport=None):
        self.account_sid = account_sid or require("TWILIO_ACCOUNT_SID")
        self.auth_token = auth_token or require("TWILIO_AUTH_TOKEN")
        self.messaging_service_sid = config("TWILIO_MESSAGING_SERVICE_SID")
        self.client = httpx.Client(
            base_url=f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/",
            auth=(self.account_sid, self.auth_token), timeout=TIMEOUT, transport=transport,
        )

    def _call(self, method, path, **kwargs):
        return request(self.client, method, path, provider=PROVIDER, **kwargs).json()

    def search_numbers(self, *, country, area_code=None, limit=5):
        country = (country or "").upper()
        if len(country) != 2 or not country.isalpha():
            raise InvalidRequest(fields={"country": "Two-letter country code, e.g. US."})
        params = {"SmsEnabled": "true", "PageSize": max(1, min(int(limit), 20))}
        if area_code:
            params["AreaCode"] = str(area_code)
        body = self._call("GET", f"AvailablePhoneNumbers/{country}/Local.json", params=params)
        return [n["phone_number"] for n in body.get("available_phone_numbers", [])]

    def buy_number(self, number, *, webhook_url, label):
        # ``label`` (FriendlyName) lets find_number() recover the outcome of a timed-out purchase.
        body = self._call("POST", "IncomingPhoneNumbers.json",
                          data={"PhoneNumber": number, "SmsUrl": webhook_url, "SmsMethod": "POST",
                                "FriendlyName": label})
        return PhoneNumber(provider_id=body["sid"], number=body["phone_number"])

    def find_number(self, label):
        body = self._call("GET", "IncomingPhoneNumbers.json", params={"FriendlyName": label})
        numbers = body.get("incoming_phone_numbers", [])
        return (PhoneNumber(provider_id=numbers[0]["sid"], number=numbers[0]["phone_number"])
                if numbers else None)

    def send(self, *, from_number, to, body, status_url):
        data = {"To": to, "Body": body, "StatusCallback": status_url}
        if self.messaging_service_sid:
            data["MessagingServiceSid"] = self.messaging_service_sid
        else:
            data["From"] = from_number
        result = self._call("POST", "Messages.json", data=data)
        return SentMessage(provider_message_id=result["sid"])

    def parse_webhook(self, url, params, headers):
        verify_twilio(self.auth_token, url, params, headers)
        sid = params.get("MessageSid") or params.get("SmsSid") or ""
        status = params.get("MessageStatus") or params.get("SmsStatus") or ""
        if status in STATUS:
            return [StatusUpdate(event_id=f"{sid}:{status}", provider_message_id=sid,
                                 status=STATUS[status], detail=params.get("ErrorCode", ""))]
        if params.get("Body") is not None and params.get("From"):
            return [InboundMessage(event_id=sid, to_address=params.get("To", ""),
                                   from_address=params.get("From", ""), body=params.get("Body", ""),
                                   provider_message_id=sid)]
        return []

    def number_price_cents(self, country):
        return int(config("SMS_NUMBER_MONTHLY_CENTS", 115))

    def message_price_cents(self):
        return int(config("SMS_MESSAGE_CENTS", 1))
