"""Cloudflare Registrar (API beta) and DNS.

Registrar endpoints (per developers.cloudflare.com/registrar/registrar-api):
``GET  accounts/{id}/registrar/domain-search?q=&limit=``,
``POST accounts/{id}/registrar/domain-check`` (up to 20 names),
``POST accounts/{id}/registrar/registrations`` (billable, non-refundable;
201 done or 202 in progress), ``GET .../registrations/{name}/registration-status``.
Beta limits: subset of TLDs, no renewals/transfers via API yet. Registrations
use the account's default registrant contact and payment method.

The API token needs Registrar write plus Zone/DNS edit for the account.
"""

from decimal import Decimal, InvalidOperation

import httpx

from ..errors import ProviderError
from . import DomainQuote, Registration, require
from .http import TIMEOUT, request

BASE = "https://api.cloudflare.com/client/v4/"
PROVIDER = "Cloudflare"


def _cents(value):
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except (InvalidOperation, TypeError):
        return 0


def _quote(item):
    pricing = item.get("pricing") or {}
    return DomainQuote(
        name=str(item.get("name", "")).lower(),
        registrable=bool(item.get("registrable")),
        price_cents=_cents(pricing.get("registration_cost")),
        renewal_cents=_cents(pricing.get("renewal_cost")),
        currency=str(pricing.get("currency") or "USD"),
        reason=str(item.get("reason") or ""),
    )


def _result(response):
    body = response.json()
    if not body.get("success", False):
        codes = [str(e.get("code", "")) for e in body.get("errors", []) if isinstance(e, dict)]
        raise ProviderError(f"{PROVIDER} request was not successful.",
                            provider_code=",".join(codes)[:60])
    return body.get("result")


class _Client:
    def __init__(self, *, token=None, account_id=None, transport=None):
        self.account_id = account_id or require("CLOUDFLARE_ACCOUNT_ID")
        self.client = httpx.Client(
            base_url=BASE, timeout=TIMEOUT, transport=transport,
            headers={"Authorization": f"Bearer {token or require('CLOUDFLARE_API_TOKEN')}"},
        )

    def call(self, method, path, **kwargs):
        return _result(request(self.client, method, path, provider=PROVIDER, **kwargs))


class CloudflareRegistrar(_Client):
    def search(self, query, limit=10):
        result = self.call("GET", f"accounts/{self.account_id}/registrar/domain-search",
                           params={"q": query, "limit": max(1, min(int(limit), 20))})
        return [_quote(item) for item in (result or {}).get("domains", [])]

    def check(self, names):
        result = self.call("POST", f"accounts/{self.account_id}/registrar/domain-check",
                           json={"domains": list(names)[:20]})
        return [_quote(item) for item in (result or {}).get("domains", [])]

    def register(self, name, *, auto_renew):
        # Asynchronous: 202 is expected; never re-POST for the same name, poll status().
        result = self.call("POST", f"accounts/{self.account_id}/registrar/registrations",
                           json={"domain_name": name, "auto_renew": bool(auto_renew)},
                           headers={"Prefer": "respond-async"})
        return self._registration(name, result)

    def status(self, name):
        result = self.call(
            "GET", f"accounts/{self.account_id}/registrar/registrations/{name}/registration-status")
        return self._registration(name, result)

    @staticmethod
    def _registration(name, result):
        result = result or {}
        error = result.get("error") or {}
        return Registration(name=name, state=str(result.get("state") or "in_progress"),
                            detail=str(error.get("code") or "")[:100])


class CloudflareDNS(_Client):
    def ensure_zone(self, name):
        zones = self.call("GET", "zones", params={"name": name, "account.id": self.account_id})
        if zones:
            return zones[0]["id"]
        zone = self.call("POST", "zones", json={"name": name, "account": {"id": self.account_id},
                                                "type": "full"})
        return zone["id"]

    def upsert_records(self, zone_id, records):
        """Idempotent: identical records are left alone; singleton names are replaced.

        CNAME, DMARC (``_dmarc``) and DKIM (``._domainkey``) names may hold only one
        value, so an existing different value is updated. MX and other TXT records
        (e.g. SPF alongside site verification) are added next to existing ones.
        """
        for record in records:
            existing = self.call("GET", f"zones/{zone_id}/dns_records",
                                 params={"type": record.type, "name": record.name}) or []
            if any(_same(r.get("content"), record.value) for r in existing):
                continue
            payload = {"type": record.type, "name": record.name, "content": record.value, "ttl": 1}
            if record.priority is not None:
                payload["priority"] = record.priority
            singleton = (record.type == "CNAME" or record.name.split(".")[0] == "_dmarc"
                         or "._domainkey" in record.name)
            if existing and singleton:
                self.call("PUT", f"zones/{zone_id}/dns_records/{existing[0]['id']}", json=payload)
            else:
                self.call("POST", f"zones/{zone_id}/dns_records", json=payload)


def _same(current, wanted):
    def norm(value):
        return str(value or "").replace('" "', "").strip('"').rstrip(".").lower()

    return norm(current) == norm(wanted)
