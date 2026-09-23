#!/usr/bin/env python3
"""Opt-in account setup through a loopback IAP tunnel, never public ingress.

Preview uses unauthenticated GETs only. --apply permits first-run creation and
login verification, not password resets. No response, cookie, credential or raw
exception is printed. Keep the public gate closed; no provider configuration.
Mautic installation additionally requires independent empty-database proof and
is deliberately not exposed as an unchecked CLI flag (see mautic_admin.py).
"""
import argparse
import base64
from dataclasses import dataclass
import http.client
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

HOSTS = {"postiz": "social.makemoredigital.com", "actual": "budget.makemoredigital.com",
         "grafana": "metrics.makemoredigital.com", "invoice_ninja": "invoices.makemoredigital.com",
         "mautic": "marketing.makemoredigital.com"}
ERROR = "Private account operation failed; response and credentials withheld."


def require(value):
    if not value:
        raise RuntimeError(ERROR)


@dataclass(repr=False)
class Response:
    status: int
    headers: object
    body: bytes

    def json(self):
        return json.loads(self.body)


class TunnelConnection(http.client.HTTPSConnection):
    def __init__(self, host, tunnel_port):
        super().__init__(host, timeout=90, context=ssl.create_default_context())
        self.tunnel_port = tunnel_port

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.tunnel_port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class Client:
    """Host/SNI stay canonical on 443; only the TCP destination is remapped.

    One cookie jar per application; no redirects, proxy environment, disk cookie
    files, debug logging, or insecure TLS switch. Responses stay in process memory.
    """
    def __init__(self, host, port=18443):
        require(host in HOSTS.values() and type(port) is int and 1024 <= port <= 65535)
        self.host, self.port = host, port
        self.origin = "https://" + host
        self.cookies = http.cookiejar.CookieJar()

    def request(self, method, path, data=None, form=None, headers=None):
        require(method in ("GET", "POST", "PUT"))
        require(path.startswith("/") and not path.startswith("//") and "\\" not in path)
        require(not any(ord(c) < 32 for c in path) and "#" not in path)
        require(data is None or form is None)
        outgoing = {"Accept": "application/json", "User-Agent": "business-tools-private-setup"}
        outgoing.update(headers or {})
        require(not any(k.lower() in ("host", "cookie") for k in outgoing))
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            outgoing["Content-Type"] = "application/json"
        if form is not None:
            body = urlencode(form).encode()
            outgoing["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(self.origin + path, data=body, headers=outgoing, method=method)
        self.cookies.add_cookie_header(request)
        connection = TunnelConnection(self.host, self.port)
        try:
            connection.request(method, path, body=body, headers=dict(request.header_items()))
            response = connection.getresponse()
            self.cookies.extract_cookies(response, request)
            content = response.read(8 * 1024 * 1024 + 1)
            require(len(content) <= 8 * 1024 * 1024)
            return Response(response.status, response.headers, content)
        finally:
            connection.close()


def protected_json(path):
    """No symlinks, public permissions or secret values in diagnostics."""
    path = Path(path).absolute()
    for parent in path.parents:
        require(not parent.is_symlink())
    info = path.parent.stat()
    require(info.st_uid == os.geteuid() and not info.st_mode & 0o077)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077)
        return json.load(stream)


def good_json(response):
    require(response.status == 200)
    return response.json()


def postiz(client, credentials=None, apply=False):
    state = good_json(client.request("GET", "/api/auth/can-register"))
    require(type(state.get("register")) is bool)
    if not apply:
        return {"preview": True, "registration_available": state["register"]}
    body = {"email": credentials["email"], "password": credentials["password"], "provider": "LOCAL"}
    if state["register"]:
        created = good_json(client.request("POST", "/api/auth/register",
                                          data={**body, "company": credentials["company"]}))
        require(created.get("register") is True and not created.get("activate"))
    require(good_json(client.request("POST", "/api/auth/login", data=body)).get("login") is True)
    user = good_json(client.request("GET", "/api/user/self"))
    require(user.get("email") == credentials["email"] and user.get("role") in ("ADMIN", "SUPERADMIN"))
    require(good_json(client.request("GET", "/api/auth/can-register"))["register"] is False)
    auth = [c for c in client.cookies if c.name == "auth"]
    require(len(auth) == 1 and auth[0].secure and auth[0].has_nonstandard_attr("HttpOnly"))
    return {"login_verified": True, "administrator": True, "registration_closed": True,
            "secure_httponly_cookie": True}


def postiz_registration_denial(client):
    """Explicitly authorized second-signup probe using a fresh private Client.

    Not part of postiz() or the CLI. Requires a closed policy and empty CookieJar;
    never logs in, resets accounts or attempts cleanup after unexpected success.
    The caller must investigate any failure without retrying account creation.
    Matches tagged v2.23.0's .status(400).send(e.message): HTTP 400 with the
    exact plain-text bytes b'Registration is disabled', not a JSON response.
    """
    try:
        require(isinstance(client.cookies, http.cookiejar.CookieJar) and len(client.cookies) == 0)
        state = good_json(client.request("GET", "/api/auth/can-register"))
        require(isinstance(state, dict) and state.get("register") is False)
        require(len(client.cookies) == 0)
        denied = client.request("POST", "/api/auth/register", data={
            # 16 random bytes keep the email local part below the DTO's 64-char limit.
            "email": "business-tools-signup-check-" + secrets.token_hex(16) + "@example.invalid",
            "password": secrets.token_urlsafe(32), "provider": "LOCAL", "company": "Security check",
        })
        require(denied.status == 400)
        require(denied.body == b"Registration is disabled")
        require(not any(cookie.name == "auth" for cookie in client.cookies))
        state = good_json(client.request("GET", "/api/auth/can-register"))
        require(isinstance(state, dict) and state.get("register") is False)
        require(not any(cookie.name == "auth" for cookie in client.cookies))
        return {"api_second_signup_denied": True}
    except Exception:
        raise RuntimeError(ERROR) from None


def actual(client, credentials=None, apply=False):
    state = good_json(client.request("GET", "/account/needs-bootstrap"))["data"]
    require(type(state.get("bootstrapped")) is bool)
    if not apply:
        return {"preview": True, "bootstrapped": state["bootstrapped"]}
    body = {"password": credentials["password"]}
    if not state["bootstrapped"]:
        require(good_json(client.request("POST", "/account/bootstrap", data=body))["status"] == "ok")
    login = good_json(client.request("POST", "/account/login", data=body))
    require(login["status"] == "ok" and login["data"]["token"])
    headers = {"X-Actual-Token": login["data"]["token"]}
    user = good_json(client.request("GET", "/account/validate", headers=headers))["data"]
    require(user.get("validated") is True and user.get("permission") == "ADMIN")
    require(user.get("loginMethod") == "password")
    denied = client.request("GET", "/sync/list-user-files")
    require(denied.status in (400, 401, 403) and denied.json().get("status") == "error")
    repeat = client.request("POST", "/account/bootstrap", data=body)
    require(repeat.status == 400 and repeat.json().get("reason") == "already-bootstrapped")
    return {"login_verified": True, "server_password_admin": True, "anonymous_files_denied": True,
            "repeat_bootstrap_denied": True}


def grafana(client, credentials=None, apply=False):
    require(client.request("GET", "/api/user").status == 401)
    if not apply:
        return {"preview": True, "anonymous_access_denied": True}
    auth = base64.b64encode((credentials["username"] + ":" + credentials["password"]).encode()).decode()
    headers = {"Authorization": "Basic " + auth}
    user = good_json(client.request("GET", "/api/user", headers=headers))
    require(user.get("isGrafanaAdmin") is True and user.get("login") == credentials["username"])
    if user.get("email") != credentials["email"]:
        require(user.get("email") in ("", "admin@localhost"))
        good_json(client.request("PUT", "/api/user", headers=headers,
                                 data={"email": credentials["email"], "name": user.get("name", ""),
                                       "login": user["login"]}))
    user = good_json(client.request("GET", "/api/user", headers=headers))
    require(user["email"] == credentials["email"])
    settings = good_json(client.request("GET", "/api/admin/settings", headers=headers))
    require(settings["auth.anonymous"]["enabled"] == "false")
    require(settings["users"]["allow_sign_up"] == "false")
    require(settings["security"]["cookie_secure"] == "true")
    health = good_json(client.request("GET", "/api/datasources/uid/business-tools-prometheus/health", headers=headers))
    require(health.get("status") == "OK")
    return {"login_verified": True, "administrator": True, "email_matches": True,
            "anonymous_and_signup_disabled": True, "secure_cookies_configured": True,
            "prometheus_datasource_healthy": True}


def invoice_ninja(client, credentials=None, apply=False):
    require(client.request("GET", "/api/v1/clients").status in (401, 403))
    setup = client.request("GET", "/setup")
    require(setup.status in (302, 303, 403, 404))
    if setup.status in (302, 303):
        target = urlsplit(setup.headers.get("Location", ""))
        require(target.scheme == "https" and target.netloc == client.host)
        require(target.path in ("", "/") and not target.query and not target.fragment)
    if not apply:
        return {"preview": True, "anonymous_clients_denied": True, "setup_unavailable": True}
    response = good_json(client.request("POST", "/api/v1/login",
                                        data={"email": credentials["email"], "password": credentials["password"]}))
    rows = response["data"]
    require(isinstance(rows, list) and len(rows) == 1)
    row = rows[0]
    require(row.get("is_owner") is True and row.get("is_admin") is True)
    require(row["user"]["email"] == credentials["email"])
    return {"login_verified": True, "owner_and_administrator": True,
            "anonymous_clients_denied": True, "setup_unavailable": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=tuple(HOSTS), required=True)
    parser.add_argument("--tunnel-port", type=int, default=18443)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        credentials = None
        if args.apply:
            require(args.credentials is not None and args.app != "mautic")
            credentials = protected_json(args.credentials)[args.app]
        client = Client(HOSTS[args.app], args.tunnel_port)
        if args.app == "mautic":
            from mautic_admin import initialize
            result = initialize(client, None, None)
        else:
            result = globals()[args.app](client, credentials, apply=args.apply)
        print(json.dumps({"app": args.app, **result}, sort_keys=True))
        return 0
    except Exception:
        print(ERROR)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())