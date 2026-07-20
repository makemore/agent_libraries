# MCP Architecture

Model Context Protocol (MCP) support in `agent_runtime_core` and
`django_agent_runtime`, target spec revision **2025-03-26**.

This document explains the split between the two packages, the shapes
of the public building blocks, and how MCP composes with the suspend
/ resume, `async_handle` and webhook primitives that already exist in
the runtime. It does **not** re-introduce mechanics that the
`CHANGELOG`s and package READMEs already document.

---

## Goals

1. Any `ToolRegistry` can be served as an MCP server with one line
   (`MCPServer(registry).serve(transport)`).
2. Any remote MCP server can be consumed as `Tool` objects via
   `mount_mcp_tools(registry, client)`.
3. Long-running MCP tool calls (on either side) surface as the same
   SUSPENDED lifecycle that `@async_handle_tool` already drives.
4. Nothing in the pure protocol path depends on Django or HTTP.

---

## Package split

```
agent_runtime_core.mcp        — pure protocol, no IO, no Django
├── protocol.py               — JSON-RPC 2.0 dataclasses + codec
├── messages.py               — typed MCP method params / results
├── capabilities.py           — ClientInfo / ServerInfo + capabilities
├── transports/               — async Transport interface
│   ├── base.py               —   InMemoryTransport (tests)
│   ├── stdio.py              —   StdioTransport (NDJSON over subprocess)
│   └── http.py               —   StreamableHttpTransport (httpx)
├── client/                   — client side
│   ├── client.py             —   MCPClient (initialize, tools/list, call_tool)
│   └── adapter.py            —   MCPToolAdapter, mount_mcp_tools
└── server/                   — server side
    ├── server.py             —   MCPServer (serves a ToolRegistry)
    └── bridge.py             —   AsyncHandleBridge

django_agent_runtime.mcp      — ASGI view + auth + persistence
├── views.py                  — McpStreamableHttpView (JSON + SSE)
├── auth.py                   — api_key + hmac auth modes
├── urls.py                   — mcp_urls() helper
└── (models in django_agent_runtime.models.mcp)
    MCPSession, MCPCallLog
```

The HTTP *client* transport lives in core (`httpx`), but the HTTP
*server* side is deliberately in the Django package because it
depends on the ASGI response type. A non-Django host can still serve
MCP over stdio or any other transport it wires up itself.

---

## How MCP composes with existing primitives

### Suspend / resume

When a tool registered on an `MCPServer` returns a suspend envelope
(produced by `@async_handle_tool` / `make_suspend_envelope`), the
server does **not** close the JSON-RPC request. Instead it:

1. Registers the pending call on the `AsyncHandleBridge` under its
   `resume_token_hash`.
2. Emits `notifications/progress` with the envelope's `reason` so
   the peer knows the call is in-flight.
3. Returns a response only when the bridge is resolved — from a
   resume callback, a webhook, or a direct call into the bridge.

On the *client* side, `MCPToolAdapter` applies the dual of the same
convention: if a `tools/call` exceeds `suspend_after` seconds, the
adapter returns a suspend envelope via `make_suspend_envelope`. The
agentic loop raises `SuspendSignal`, the runner persists the run as
SUSPENDED, and resumption arrives through the existing resume
callback path. No new suspend mechanism is introduced.

### HMAC signing

The Django view reuses `agent_runtime_core.security`:
`verify_signature` over the raw request body, same
`X-Agent-Runtime-Signature` / `-Timestamp` / `-Key-Id` headers as
resume callbacks. `MCP_HMAC_SECRET` falls back to
`RESUME_CALLBACK_SECRET` so projects that already configured
cross-service delegation get HMAC-auth on MCP for free.

### Webhooks and event bus

Runs that suspend inside an MCP `tools/call` still fan `run.suspended`
out through `AgentWebhookSubscription` / `fanout_event`. Nothing in
the MCP package subscribes to the event bus directly; it is the run
lifecycle that triggers fan-out, and that lifecycle is unchanged.

### TOOL_PROGRESS event

`EventType.TOOL_PROGRESS` (added in 0.12.0) is emitted by
`MCPToolAdapter` each time the remote server sends
`notifications/progress`. It sits alongside the existing
`tool.call` / `tool.result` events; runners forward it to whatever
sink they already use (SSE, event bus, Langfuse, etc.) without
special-casing.

---

## Transports and the fast-path / SSE split (Django view)

`McpStreamableHttpView` implements the MCP "Streamable HTTP" server
pattern:

- Synchronous tools that return within `MCP_FAST_PATH_TIMEOUT_SECONDS`
  are answered with a single JSON response (HTTP 200,
  `application/json`).
- Tools that exceed the fast-path timeout, or that return a suspend
  envelope, switch the response to an SSE stream
  (`text/event-stream`). Subsequent `notifications/progress` and the
  final response frame are written as `data:` events.
- The `Mcp-Session-Id` header ties subsequent requests to the same
  session row, so the `AsyncHandleBridge` state is preserved across
  the resume callback and the follow-up SSE read.

`DELETE /mcp/` closes a session by ID; this is used by peers that
want to cleanly release server-side state rather than letting it
time out.

---

## Observability

`MCPCallLog` is deliberately per-JSON-RPC-call, not per-tool. This
lets operators see `initialize`, `tools/list`, `tools/call` and the
session close events in the same table, with `status`,
`duration_ms`, `error` and `tool_name` columns for the cases that
have one. `MCPSession` rows record `auth_mode`, `protocol_version`
and `client_info` at the moment of handshake, which is usually what
you need when diagnosing a misbehaving peer.

Neither table is referenced by the hot path (dispatch reads the
tool registry, not the DB) so retention can be aggressive.
