"""Provider interfaces and host configuration.

Services only use these interfaces, so AgentMail, Cloudflare and Twilio can be
swapped. Hosts configure concrete classes and credentials in settings::

    AGENT_CHANNELS = {
        "EMAIL_PROVIDER": "django_agent_channels.providers.agentmail.AgentMailProvider",
        "REGISTRAR_PROVIDER": "django_agent_channels.providers.cloudflare.CloudflareRegistrar",
        "DNS_PROVIDER": "django_agent_channels.providers.cloudflare.CloudflareDNS",
        "SMS_PROVIDER": "django_agent_channels.providers.twilio.TwilioSMS",
        "AGENTMAIL_API_KEY": env("AGENTMAIL_API_KEY"),
        "AGENTMAIL_WEBHOOK_SECRET": env("AGENTMAIL_WEBHOOK_SECRET"),
        "CLOUDFLARE_ACCOUNT_ID": env("CLOUDFLARE_ACCOUNT_ID"),
        "CLOUDFLARE_API_TOKEN": env("CLOUDFLARE_API_TOKEN"),
        "TWILIO_ACCOUNT_SID": env("TWILIO_ACCOUNT_SID"),
        "TWILIO_AUTH_TOKEN": env("TWILIO_AUTH_TOKEN"),
        "TWILIO_MESSAGING_SERVICE_SID": env("TWILIO_MESSAGING_SERVICE_SID"),  # optional
        "PUBLIC_BASE_URL": "https://studio.example.com",  # webhooks point here
        "DEFAULT_EMAIL_DOMAIN": "agents.example.com",     # optional
    }

Unset providers mean that capability is unavailable (``NotConfigured``), never
silently faked.
"""

from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings
from django.utils.module_loading import import_string

from ..errors import NotConfigured


def config(name, default=None):
    return getattr(settings, "AGENT_CHANNELS", {}).get(name, default)


def require(name):
    value = config(name)
    if not value:
        raise NotConfigured(f"{name} is not configured.")
    return value


# --------------------------------------------------------------------------- value types

@dataclass(frozen=True)
class DomainQuote:
    name: str
    registrable: bool
    price_cents: int = 0
    renewal_cents: int = 0
    currency: str = "USD"
    reason: str = ""


@dataclass(frozen=True)
class Registration:
    name: str
    state: str  # in_progress | succeeded | failed | action_required | blocked
    detail: str = ""


@dataclass(frozen=True)
class DnsRecord:
    type: str
    name: str
    value: str
    priority: int | None = None


@dataclass(frozen=True)
class EmailDomain:
    provider_id: str
    status: str  # provider status, normalised to upper case
    records: tuple = ()


@dataclass(frozen=True)
class Inbox:
    provider_id: str
    address: str


@dataclass(frozen=True)
class SentMessage:
    provider_message_id: str
    thread_ref: str = ""


@dataclass(frozen=True)
class PhoneNumber:
    provider_id: str
    number: str  # E.164
    monthly_cents: int = 0


@dataclass(frozen=True)
class InboundMessage:
    """Normalised inbound email or SMS from a verified webhook."""

    event_id: str
    to_address: str
    from_address: str
    subject: str = ""
    body: str = ""
    provider_message_id: str = ""
    thread_ref: str = ""
    in_reply_to: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StatusUpdate:
    event_id: str
    provider_message_id: str
    status: str  # sent | delivered | failed | bounced
    detail: str = ""


# --------------------------------------------------------------------------- interfaces

class RegistrarProvider(Protocol):
    def search(self, query: str, limit: int = 10) -> list[DomainQuote]: ...
    def check(self, names: list[str]) -> list[DomainQuote]: ...
    def register(self, name: str, *, auto_renew: bool) -> Registration: ...
    def status(self, name: str) -> Registration: ...


class DnsProvider(Protocol):
    def ensure_zone(self, name: str) -> str: ...
    def upsert_records(self, zone_id: str, records: list[DnsRecord]) -> None: ...


class EmailProvider(Protocol):
    def add_domain(self, name: str, *, client_id: str) -> EmailDomain: ...
    def get_domain(self, provider_id: str) -> EmailDomain: ...
    def verify_domain(self, provider_id: str) -> None: ...
    def create_inbox(self, *, username: str, domain: str | None, display_name: str,
                     client_id: str) -> Inbox: ...
    def send(self, *, inbox_id: str, to: list[str], subject: str, text: str,
             cc: list[str], bcc: list[str], idempotency_key: str) -> SentMessage: ...
    def reply(self, *, inbox_id: str, message_id: str, text: str,
              idempotency_key: str) -> SentMessage: ...
    def parse_webhook(self, body: bytes, headers: dict) -> list: ...


class SmsProvider(Protocol):
    def search_numbers(self, *, country: str, area_code: str | None, limit: int) -> list[str]: ...
    def buy_number(self, number: str, *, webhook_url: str, label: str) -> PhoneNumber: ...
    def find_number(self, label: str) -> PhoneNumber | None: ...
    def send(self, *, from_number: str, to: str, body: str, status_url: str) -> SentMessage: ...
    def parse_webhook(self, url: str, params: dict, headers: dict) -> list: ...
    def number_price_cents(self, country: str) -> int: ...
    def message_price_cents(self) -> int: ...


def _load(setting):
    path = config(setting)
    if not path:
        raise NotConfigured(f"{setting} is not configured.")
    cls = import_string(path) if isinstance(path, str) else path
    return cls() if isinstance(cls, type) else cls


def email_provider() -> EmailProvider:
    return _load("EMAIL_PROVIDER")


def registrar_provider() -> RegistrarProvider:
    return _load("REGISTRAR_PROVIDER")


def dns_provider() -> DnsProvider:
    return _load("DNS_PROVIDER")


def sms_provider() -> SmsProvider:
    return _load("SMS_PROVIDER")
