#!/usr/bin/env python3
"""Loopback-only IAP assertion verifier for Caddy forward_auth; never a proxy."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import re
from socketserver import ThreadingMixIn
import sys
import threading
import time
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt
from jwt.utils import base64url_decode, base64url_encode


CONFIG_PATH = Path("/usr/local/lib/ai-gateway/iap-auth.json")
KEY_URL = "https://www.gstatic.com/iap/verify/public_key"
ISSUER = "https://cloud.google.com/iap"
ASSERTION_HEADER = "X-Goog-Iap-Jwt-Assertion"
LISTEN_ADDRESS = ("127.0.0.1", 9091)
MAX_TOKEN_BYTES = 16384
MAX_RESPONSE_BYTES = 128 * 1024
KEY_TTL = 3600
REFRESH_INTERVAL = 60
NETWORK_TIMEOUT = 5
SOCKET_TIMEOUT = 5
MAX_WORKERS = 16
MAX_LIFETIME = 660
LEEWAY = 30
AUDIENCE = re.compile(r"/projects/[0-9]+/global/backendServices/[0-9]+")
KID = re.compile(r"[A-Za-z0-9_-]{1,256}")
TOKEN = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
REQUIRED_CLAIMS = ("sub", "email", "iss", "aud", "exp", "iat")


class InvalidAssertion(Exception):
    """An assertion is absent, malformed, untrusted, or unauthorized."""


class KeysUnavailable(Exception):
    """No usable key set is available; never attach upstream error details."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Invalid JSON number")


def parse_json(data):
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=reject_constant)


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"enabled", "audience", "allowed_emails"}:
        raise ValueError("Invalid configuration")
    enabled, audience, emails = config["enabled"], config["audience"], config["allowed_emails"]
    if type(enabled) is not bool:
        raise ValueError("Invalid enabled flag")
    if audience is not None and (not isinstance(audience, str) or not AUDIENCE.fullmatch(audience)):
        raise ValueError("Invalid audience")
    if not isinstance(emails, list) or any(
        not isinstance(email, str) or len(email) > 254
        or not EMAIL.fullmatch(email) or ".." in email for email in emails
    ):
        raise ValueError("Invalid email allowlist")
    if len(set(emails)) != len(emails) or (enabled and (audience is None or not emails)):
        raise ValueError("Invalid configuration")
    return config


def load_config(path=CONFIG_PATH):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Configuration too large")
    return validate_config(parse_json(data.decode("utf-8")))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_public_keys(opener=None):
    if opener is None:
        # Use default HTTPS certificate/hostname verification, never host proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(KEY_URL), timeout=NETWORK_TIMEOUT) as response:
        if response.status != 200:
            raise ValueError("Invalid key response")
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Key response too large")
    keys = parse_json(data.decode("utf-8"))
    if not isinstance(keys, dict) or not keys or len(keys) > 256:
        raise ValueError("Invalid key mapping")
    for kid, pem in keys.items():
        if not KID.fullmatch(kid) or not isinstance(pem, str) or len(pem) > 4096:
            raise ValueError("Invalid key mapping")
        if not pem.startswith("-----BEGIN PUBLIC KEY-----\n"):
            raise ValueError("Invalid public key")
        key = serialization.load_pem_public_key(pem.encode("ascii"))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("Invalid public key")
    return keys


class KeyCache:
    def __init__(self, opener=None, clock=time.monotonic):
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._keys = {}
        self._expires = 0
        self._last_attempt = -math.inf
        self._refresh_failed = False

    def get(self, kid):
        # Serialize refreshes, including failures and unknown-kid requests.
        with self._lock:
            now = self._clock()
            fresh = now < self._expires
            if fresh and kid in self._keys:
                return self._keys[kid]
            if now - self._last_attempt < REFRESH_INTERVAL:
                if not fresh or self._refresh_failed:
                    raise KeysUnavailable()
                raise InvalidAssertion()
            self._last_attempt = now
            try:
                keys = fetch_public_keys(self._opener)
            except Exception:
                self._refresh_failed = True
                raise KeysUnavailable() from None
            self._keys = keys
            self._expires = self._clock() + KEY_TTL
            self._refresh_failed = False
            if kid not in keys:
                raise InvalidAssertion()
            return keys[kid]


class Verifier:
    def __init__(self, config, keys=None):
        config = validate_config(config)
        self.enabled = config["enabled"]
        self.audience = config["audience"]
        self.allowed_emails = frozenset(config["allowed_emails"])
        self.keys = keys if keys is not None else KeyCache()

    def _validate_claims(self, claims):
        if not isinstance(claims, dict) or any(name not in claims for name in REQUIRED_CLAIMS):
            raise InvalidAssertion()
        # PyJWT 2.7 accepts list audiences and coerces numeric dates; be stricter.
        if not isinstance(claims["aud"], str) or claims["aud"] != self.audience:
            raise InvalidAssertion()
        if claims["iss"] != ISSUER or not isinstance(claims["sub"], str) or not claims["sub"].strip():
            raise InvalidAssertion()
        if not isinstance(claims["email"], str) or claims["email"] not in self.allowed_emails:
            raise InvalidAssertion()
        for name in ("exp", "iat", "nbf"):
            if name in claims and (type(claims[name]) not in (int, float) or not math.isfinite(claims[name])):
                raise InvalidAssertion()
        if not 0 < claims["exp"] - claims["iat"] <= MAX_LIFETIME:
            raise InvalidAssertion()

    def verify(self, token):
        """Return None on success; raise only sanitized authentication/key errors."""
        try:
            if not self.enabled or not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES or not TOKEN.fullmatch(token):
                raise InvalidAssertion()
            segments = token.split(".")
            decoded = [base64url_decode(segment) for segment in segments]
            if any(base64url_encode(value).decode("ascii") != segment for value, segment in zip(decoded, segments)):
                raise InvalidAssertion()
            header = parse_json(decoded[0].decode("utf-8"))
            claims = parse_json(decoded[1].decode("utf-8"))
            if not isinstance(header, dict) or header.get("alg") != "ES256":
                raise InvalidAssertion()
            kid = header.get("kid")
            if not isinstance(kid, str) or not KID.fullmatch(kid) or "crit" in header or "b64" in header:
                raise InvalidAssertion()
            self._validate_claims(claims)
            # Only this locally fetched PEM is trusted. jku/jwk/x5u cannot select
            # a network location or a key, and signature failures never refresh.
            claims = jwt.decode(
                token, self.keys.get(kid), algorithms=["ES256"],
                audience=self.audience, issuer=ISSUER, leeway=LEEWAY,
                options={"require": list(REQUIRED_CLAIMS)},
            )
            self._validate_claims(claims)
            # Preserve exact numeric-date boundaries, even for fractional dates
            # that PyJWT's integer conversion would round down.
            now = time.time()
            if claims["exp"] <= now - LEEWAY or claims["iat"] > now + LEEWAY:
                raise InvalidAssertion()
            if "nbf" in claims and claims["nbf"] > now + LEEWAY:
                raise InvalidAssertion()
        except KeysUnavailable:
            raise
        except Exception:
            raise InvalidAssertion() from None


class AuthHandler(BaseHTTPRequestHandler):
    timeout = SOCKET_TIMEOUT

    def log_message(self, format, *args):
        pass

    def log_error(self, format, *args):
        pass

    def _respond(self, status):
        self.close_connection = True
        self.send_response_only(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def send_error(self, code, message=None, explain=None):
        # No default HTML body reflecting request targets, methods or errors.
        self._respond(405 if code == 501 else code)

    def do_GET(self):
        # Check the original target too: BaseHTTPRequestHandler normalizes //.
        if self.path != "/verify" or self.requestline.split()[1] != "/verify":
            self._respond(404)
            return
        assertions = self.headers.get_all(ASSERTION_HEADER, [])
        if len(assertions) != 1:
            self._respond(401)
            return
        try:
            self.server.verifier.verify(assertions[0])
        except KeysUnavailable:
            status = 503
        except Exception:
            status = 401
        else:
            status = 200
        self._respond(status)


class AuthServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_WORKERS

    def __init__(self, verifier):
        self.verifier = verifier
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)
        super().__init__(LISTEN_ADDRESS, AuthHandler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(SOCKET_TIMEOUT)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        except Exception:
            # Also suppress failures from socketserver's final socket cleanup;
            # an uncaught worker exception would invoke threading.excepthook.
            pass
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # The default socketserver traceback could disclose request data.
        pass


def main(config_path=CONFIG_PATH):
    try:
        config = load_config(config_path)
        if not config["enabled"]:
            return 0
        with AuthServer(Verifier(config)) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Never print configuration, exception messages or remote responses.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main() if len(sys.argv) == 1 else 1)