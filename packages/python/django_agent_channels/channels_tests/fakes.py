"""In-memory providers matching the provider interfaces. Tests can inject failures."""

from django_agent_channels.errors import ProviderError
from django_agent_channels.providers import (
    DnsRecord,
    DomainQuote,
    EmailDomain,
    Inbox,
    PhoneNumber,
    Registration,
    SentMessage,
)


class State:
    def __init__(self):
        self.prices = {}             # domain -> cents; missing = unavailable
        self.registered = {}         # domain -> state
        self.register_calls = []
        self.register_error = None   # ProviderError to raise once
        self.registration_state = "succeeded"
        self.zones = {}
        self.records = {}            # zone -> list[DnsRecord]
        self.email_domains = {}      # id -> status
        self.inboxes = {}
        self.sent = []               # (kind, key, payload)
        self.send_keys = {}          # AgentMail Idempotency-Key -> SentMessage
        self.send_error = None
        self.numbers_available = ["+14155550100", "+14155550101"]
        self.numbers = {}            # label -> PhoneNumber
        self.buy_error = None
        self.sms = []
        self.sms_error = None


STATE = State()


def reset():
    STATE.__init__()


def _raise(attr):
    error = getattr(STATE, attr)
    if error is not None:
        setattr(STATE, attr, None)
        raise error


class FakeRegistrar:
    def search(self, query, limit=10):
        return self.check([n for n in STATE.prices if query.replace(" ", "") in n][:limit])

    def check(self, names):
        out = []
        for name in names:
            if name in STATE.prices and name not in STATE.registered:
                out.append(DomainQuote(name, True, STATE.prices[name], STATE.prices[name]))
            else:
                out.append(DomainQuote(name, False, reason="domain_unavailable"))
        return out

    def register(self, name, *, auto_renew):
        STATE.register_calls.append((name, auto_renew))
        if STATE.register_error is not None:
            error, STATE.register_error = STATE.register_error, None
            if error.provider_code == "timeout":
                STATE.registered[name] = STATE.registration_state  # it actually went through
            raise error
        STATE.registered[name] = STATE.registration_state
        return Registration(name, STATE.registration_state)

    def status(self, name):
        return Registration(name, STATE.registered.get(name, "failed"))


class FakeDNS:
    def ensure_zone(self, name):
        return STATE.zones.setdefault(name, f"zone-{name}")

    def upsert_records(self, zone_id, records):
        existing = STATE.records.setdefault(zone_id, [])
        for record in records:
            if record not in existing:
                existing.append(record)


class FakeEmail:
    def add_domain(self, name, *, client_id):
        STATE.email_domains.setdefault(name, "PENDING")
        return EmailDomain(name, STATE.email_domains[name], (
            DnsRecord("TXT", "_dmarc", "v=DMARC1; p=reject"),
            DnsRecord("MX", name, "inbound.agentmail.test", 10),
            DnsRecord("TXT", f"agentmail._domainkey.{name}", "v=DKIM1; k=rsa; p=abc"),
        ))

    def get_domain(self, provider_id):
        return EmailDomain(provider_id, STATE.email_domains.get(provider_id, "NOT_STARTED"))

    def verify_domain(self, provider_id):
        pass

    def create_inbox(self, *, username, domain, display_name, client_id):
        address = f"{username}@{domain or 'agentmail.to'}"
        STATE.inboxes[client_id] = address
        return Inbox(provider_id=address, address=address)

    def send(self, *, inbox_id, to, subject, text, cc, bcc, idempotency_key):
        return self._send("send", idempotency_key, {"inbox": inbox_id, "to": to, "cc": cc, "bcc": bcc,
                                                     "subject": subject, "text": text})

    def reply(self, *, inbox_id, message_id, text, idempotency_key):
        return self._send("reply", idempotency_key, {"inbox": inbox_id, "reply_to": message_id, "text": text})

    def _send(self, kind, key, payload):
        if key in STATE.send_keys:  # provider-side idempotency, like AgentMail
            return STATE.send_keys[key]
        if STATE.send_error is not None:
            error, STATE.send_error = STATE.send_error, None
            raise error
        STATE.sent.append((kind, key, payload))
        result = SentMessage(f"<m{len(STATE.sent)}@agentmail.test>", f"thread-{len(STATE.sent)}")
        STATE.send_keys[key] = result
        return result

    def parse_webhook(self, body, headers):
        raise NotImplementedError


class FakeSMS:
    def search_numbers(self, *, country, area_code=None, limit=5):
        return STATE.numbers_available[:limit]

    def buy_number(self, number, *, webhook_url, label):
        if STATE.buy_error is not None:
            error, STATE.buy_error = STATE.buy_error, None
            if error.provider_code == "timeout":
                STATE.numbers[label] = PhoneNumber(f"PN{len(STATE.numbers)}", number)
            raise error
        bought = PhoneNumber(f"PN{len(STATE.numbers)}", number)
        STATE.numbers[label] = bought
        STATE.numbers_available.remove(number)
        return bought

    def find_number(self, label):
        return STATE.numbers.get(label)

    def send(self, *, from_number, to, body, status_url):
        _raise("sms_error")
        STATE.sms.append((from_number, to, body))
        return SentMessage(f"SM{len(STATE.sms)}")

    def parse_webhook(self, url, params, headers):
        raise NotImplementedError

    def number_price_cents(self, country):
        return 115

    def message_price_cents(self):
        return 1


def timeout():
    return ProviderError("Timed out.", retriable=True, provider_code="timeout")


def refused():
    return ProviderError("Refused.", retriable=False, provider_code="400")
