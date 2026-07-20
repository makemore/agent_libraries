# MCP Deployment and Authentication

Practical guide for deploying the `django_agent_runtime.mcp`
streamable-HTTP endpoint and authenticating clients against it.

The design — and how MCP composes with suspend/resume, HMAC and
webhooks — is documented in [`mcp-architecture.md`](./mcp-architecture.md).
This doc covers only the operational story: settings, URLs, auth
modes and client usage.

---

## 1. Prerequisites

- `django-agent-runtime>=0.12.0` (which requires
  `agent-runtime-core>=0.12.0`).
- An **ASGI** worker (`uvicorn`, `daphne`, `hypercorn`). The MCP view
  is async and the SSE path uses `StreamingHttpResponse` with an
  async generator; WSGI will not work.
- The runtime's existing migrations must be applied — the MCP
  migration `0031_mcp_session_and_call_log` adds
  `agent_runtime_mcp_session` and `agent_runtime_mcp_call_log`.

---

## 2. Server setup

### 2.1 Settings

All MCP settings live under the existing `DJANGO_AGENT_RUNTIME` dict.
Nothing new to register at the Django app level.

```python
# settings.py
DJANGO_AGENT_RUNTIME = {
    # ... existing runtime settings ...

    # Master switch. With False, the endpoint returns 503 for every
    # request — useful for staged rollouts.
    "MCP_ENABLED": True,

    # Which auth modes to accept. See §3 below. Default is
    # ["api_key", "hmac"].
    "MCP_AUTH_MODES": ["api_key", "hmac"],

    # API keys accepted in the X-MCP-Api-Key header. Keys are
    # {key_id: secret}; the id is recorded on MCPSession and logged.
    "MCP_API_KEYS": {
        "partner-acme": env("MCP_API_KEY_ACME"),
        "partner-globex": env("MCP_API_KEY_GLOBEX"),
    },

    # Shared secret for HMAC-signed requests. Falls back to
    # RESUME_CALLBACK_SECRET when unset, so a project that already
    # signs resume callbacks gets HMAC-auth on MCP for free.
    "MCP_HMAC_SECRET": env("MCP_HMAC_SECRET", default=None),

    # Response-mode switch. If the server does not produce a reply
    # within this many seconds, the view promotes the response from
    # JSON to SSE. Keep small — it is pure scheduler overhead.
    "MCP_FAST_PATH_TIMEOUT_SECONDS": 0.25,

    # Ceiling on SSE-mode calls. Set slightly below your reverse-
    # proxy read timeout. Default: 600s.
    "MCP_SSE_TIMEOUT_SECONDS": 300,
}
```

### 2.2 Building the `MCPServer`

Build it **once** at module scope and hand the same instance to every
request. The server holds the `AsyncHandleBridge` that correlates
suspended calls with their eventual resolution.

```python
# myproject/mcp_server.py
from agent_runtime_core.mcp import ServerInfo
from agent_runtime_core.mcp.server import MCPServer
from myproject.tools import build_registry

mcp_server = MCPServer(
    build_registry(),
    server_info=ServerInfo(name="acme-agent", version="1.0.0"),
)
```

### 2.3 URL wiring

```python
# myproject/urls.py
from django.urls import include, path
from django_agent_runtime.mcp import mcp_urls

from myproject.mcp_server import mcp_server

urlpatterns = [
    ...,
    path(
        "mcp/",
        include((mcp_urls(server=mcp_server), "mcp"), namespace="mcp"),
    ),
]
```

Use `server_factory=callable` instead of `server=` if you need
per-request server selection (e.g. tenant-scoped registries).

### 2.4 Migrations and serving

```bash
./manage.py migrate django_agent_runtime
uvicorn myproject.asgi:application --host 0.0.0.0 --port 8000
```

### 2.5 Reverse-proxy checklist

- Disable response buffering on `/mcp/` (nginx:
  `proxy_buffering off`). SSE deliveries must stream.
- Set the upstream read timeout **above**
  `MCP_SSE_TIMEOUT_SECONDS`.
- Forward the `Mcp-Session-Id`, `X-MCP-Api-Key`,
  `X-Agent-Runtime-Signature`, `X-Agent-Runtime-Timestamp` and
  `X-Agent-Runtime-Key-Id` headers verbatim (most proxies do, but
  some aggressive allow-lists strip the `X-` custom ones).

---

## 3. Authentication

The view evaluates modes in the order configured in
`MCP_AUTH_MODES` and accepts the first one that matches. Failure to
authenticate returns `401 {"error": "unauthorized"}`; server-side
misconfiguration returns `500 {"error": "auth_misconfigured"}`.

### 3.1 `api_key`

Simplest mode, suitable for machine-to-machine peers.

- Configure keys in `MCP_API_KEYS` as `{key_id: secret}`. The
  `key_id` is a label; the `secret` is the bearer value.
- The client sends its secret in the `X-MCP-Api-Key` header.
- The matched `key_id` is written to `MCPSession.auth_principal`.
- Rotate by adding a new entry, deploying clients, then removing
  the old entry. Comparison is constant-time against every
  configured key.

### 3.2 `hmac`

Body-authenticating mode. Reuses
`agent_runtime_core.security.verify_signature`, so the same
envelope already used for resume callbacks and webhooks
authenticates MCP too.

Required headers:

| Header | Meaning |
|---|---|
| `X-Agent-Runtime-Signature` | `v1=<hex>` where `<hex>` is `HMAC-SHA256(secret, f"{timestamp}.{key_id}.{body}")` |
| `X-Agent-Runtime-Timestamp` | Unix seconds as a string |
| `X-Agent-Runtime-Key-Id` | Optional; identifies which secret the signature was produced with (blank if unused) |

- The shared secret is `MCP_HMAC_SECRET`, falling back to
  `RESUME_CALLBACK_SECRET`. If neither is set and `hmac` is the
  sole auth mode, the endpoint returns `500 auth_misconfigured`.
- Clock skew is bounded by `SIGNATURE_MAX_SKEW_SECONDS` (default
  300s).
- The `key_id` from the signed headers is written to
  `MCPSession.auth_principal`.

### 3.3 `none`

Convenience mode for development: every request is authenticated as
an anonymous principal. **Only enable it behind a trusted proxy.**
It should never appear in a production `MCP_AUTH_MODES` list.

### 3.4 Combining modes

`MCP_AUTH_MODES=["api_key", "hmac"]` means "accept either". This is
useful during a migration: partners on the old API-key scheme keep
working while new ones are onboarded onto HMAC.

---

## 4. Client usage

### 4.1 Session lifecycle

1. Client `POST`s `initialize` with no session header. The server
   responds with `Mcp-Session-Id: <opaque>`, creating an
   `MCPSession` row with status `active`.
2. All subsequent requests echo `Mcp-Session-Id: <opaque>` so the
   server can correlate them with the session and, for suspended
   calls, the correct `AsyncHandleBridge` state.
3. `DELETE /mcp/` with the session header closes the session
   cleanly (status goes to `closed`, `closed_at` is stamped).
   Unused sessions time out by policy on the server side.

### 4.2 Python client — API key

```python
from agent_runtime_core.mcp import ClientInfo
from agent_runtime_core.mcp.client import MCPClient, mount_mcp_tools
from agent_runtime_core.mcp.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    "https://agent.example.com/mcp/",
    headers={"X-MCP-Api-Key": "<secret>"},
)
client = MCPClient(
    transport,
    client_info=ClientInfo(name="my-agent", version="1.0"),
)
await client.initialize()
await mount_mcp_tools(my_registry, client, suspend_after=30.0)
```

### 4.3 Python client — HMAC

HMAC requires a per-request signature over the raw body, so the
client supplies an `httpx.Auth` that signs on `auth_flow`:

```python
import httpx
from agent_runtime_core.security import sign_payload
from agent_runtime_core.mcp.client import MCPClient
from agent_runtime_core.mcp.transports import StreamableHttpTransport

class HmacAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, secret: str, key_id: str = ""):
        self._secret, self._key_id = secret, key_id

    def sync_auth_flow(self, request):
        sig = sign_payload(request.content or b"", self._secret, key_id=self._key_id)
        for k, v in sig.to_headers().items():
            request.headers[k] = v
        yield request

    async def async_auth_flow(self, request):
        for r in self.sync_auth_flow(request):
            yield r

http = httpx.AsyncClient(auth=HmacAuth(secret="<shared>"), timeout=30.0)
transport = StreamableHttpTransport("https://agent.example.com/mcp/", client=http)
client = MCPClient(transport)
await client.initialize()
```

### 4.4 Debugging with curl

```bash
# Smoke-test the initialize handshake with API key auth.
BODY='{"jsonrpc":"2.0","id":1,"method":"initialize",'\
'"params":{"protocolVersion":"2025-03-26","capabilities":{},'\
'"clientInfo":{"name":"curl","version":"0"}}}'

curl -sS -i https://agent.example.com/mcp/ \
  -H "Content-Type: application/json" \
  -H "X-MCP-Api-Key: $MCP_API_KEY" \
  -d "$BODY"
```

The response carries the `Mcp-Session-Id` header for subsequent
calls.

---

## 5. Observability

- `MCPSession` (`agent_runtime_mcp_session`): one row per session.
  Filter on `status`, `auth_mode`, `auth_principal`, `last_seen_at`.
- `MCPCallLog` (`agent_runtime_mcp_call_log`): one row per JSON-RPC
  request, including `initialize`, `tools/list`, `tools/call`.
  `status` transitions `pending → suspended? → succeeded | failed`;
  `error_code` and `error_message` capture failures.

Both tables are written outside the hot path and can be aggressively
pruned — nothing in dispatch reads them.

---

## 6. Disabling or decommissioning

- Toggle `MCP_ENABLED = False` to 503 the endpoint without taking
  the URL out. Existing sessions stay in the DB and can be pruned
  manually.
- Remove the `include(mcp_urls(...))` line from the URLconf to
  retire the endpoint entirely. The models and migration remain —
  roll a data migration if you want to drop the tables.

  the old entry. Comparison is constant-time against every
  configured key.
