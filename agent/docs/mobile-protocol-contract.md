# Mobile Agent Runtime Protocol Contract

This document is the product-neutral contract consumed by native iOS, Android, and web agent-chat clients. Host products may map these primitives to their own shell, but the runtime must not emit product-specific concepts.

## Endpoint inventory

| Capability | Endpoint shape | Notes |
|---|---|---|
| Create run | `POST /api/agent-runtime/runs/` | Body: `agent_key`, `conversation_id?`, `messages`, `params?`, `metadata?`, `model?`, `thinking?`, `ephemeral?`, `memories?`. Response includes stable `id`, `conversation_id`, `agent_key`, `status`, timestamps. |
| Cancel run | `POST /api/agent-runtime/runs/{run_id}/cancel/` | Marks `cancel_requested_at`; runners emit `run.cancelled` when observed. Clients should also locally transition to cancelling/cancelled after a successful 2xx response. |
| SSE stream | `GET /api/agent-runtime/runs/{run_id}/events/` | Sync DB-backed stream. Supports `?token=`, `?anonymous_token=`, `from_seq`, `include_debug`, `include_all`. |
| SSE stream (async) | `GET /api/agent-runtime/runs/{run_id}/stream/` | Async event-bus stream with the same event envelope. Uses async ORM access to avoid Django session DB errors. |
| Conversation detail | `GET /api/agent-runtime/conversations/{id}/?limit=N&offset=M` | Messages are returned in chronological order for the requested page. `offset=0&limit=N` returns the last N messages; increasing offset loads older pages. Response includes `messages`, `total_messages`, `has_more`. |
| Voice token | `POST /api/agent-runtime/voice/token/` | Returns `token`, `expires_at`, `tts_url` when voice is enabled. |
| Voice TTS | `POST /api/agent-runtime/voice/tts/` | Bearer or `X-Voice-Token`; body includes `text`, `voice_id?`, `model_id?`, `voice_settings?`, `emotion?`, `output_format?`. Streams provider audio bytes. |
| Voice voices | `GET /api/agent-runtime/voice/voices/` | Lists configured provider voices. |

## SSE envelope

Every event is emitted as a named SSE frame:

```text
event: assistant.delta
data: {"run_id":"...","seq":0,"type":"assistant.delta","payload":{},"ts":"...","visibility_level":"user","ui_visible":true}
```

`seq` is stable per run and is suitable for replay via `from_seq`. Clients should switch on `type` and read event-specific data from `payload`. Unknown event types must be preserved or ignored safely, never crash the stream parser.

## Canonical event table

| Event | Payload contract | Client behavior |
|---|---|---|
| `run.started` | `{agent_key, attempt}` | Transition `sending -> streaming`. |
| `assistant.delta` | `{delta, emotion?}` | Append token text to current assistant bubble. |
| `assistant.message` | `{content, role?, emotion?}` | Authoritative full assistant message; replace accumulated deltas for that turn. |
| `tool.call` | Canonical `{id, name, arguments}`; legacy aliases `{tool_call_id, tool_name, tool_args}` may appear. | Render a tool-call row/card. |
| `tool.result` | Canonical `{tool_call_id, name, result}`; legacy alias `tool_name` may appear. | Render tool result; inspect `result.error` for failed tool UI. |
| `tool.progress` | Free-form progress payload. | Optional progress UI. |
| `content.blocks` | `{tool_name?, tool_call_id?, blocks:[...]}` | Render rich content blocks without product-specific assumptions. |
| `client.action.required` | See required-action payload below. | Render a reusable action card and transition to `waiting`; live stream may complete here. |
| `run.suspended` | `{reason, tool_name?, tool_call_id?, external_handle?, required_action?}` | Transition to `waiting`; stream closes even though the run may later resume. |
| `run.cancelled` | `{}` | Terminal run state; stop spinners and TTS. |
| `run.failed` | `{error, error_type?, error_details?, attempt?, max_attempts?, retriable?}` | Terminal failure; show error and retry affordance if applicable. |
| `run.succeeded` | `{output?, usage?}` | Terminal success; finish/drain current assistant bubble. |
| `run.timed_out` | `{timeout_seconds}` | Terminal failure-like state. |
| `run.heartbeat` | `{}` | Internal keepalive/progress signal; normally hidden. |
| `state.checkpoint`, `step.*`, `progress.update`, `memory.update` | Event-specific metadata. | Optional UI; must not block terminal state handling. |

No standard supersede/update event is emitted today. Edit/retry flows are represented at run creation (`supersede_from_message_index`) and persisted via `superseded_by`; clients should treat this as a backend-dependent gap unless a future `run.superseded`/`timeline.updated` event is added.

## Required action payload

`client.action.required` is generic so host apps can use it for OAuth, permissions, human approval, payment auth, or other out-of-band steps:

```json
{
  "action_id": "act-123",
  "action_type": "oauth",
  "title": "Connect account",
  "message": "Connect an account so the agent can continue.",
  "action_url": "https://example.invalid/connect",
  "action_label": "Connect",
  "metadata": {"provider": "example"},
  "resume_hint": {"strategy": "resume_run"}
}
```

`run.suspended` may include the same object under `required_action` when the suspension is tied to a user-visible action.

## Persistence expectations

- Persisted conversation messages must be reconstructable without replaying SSE.
- Assistant messages use role `assistant`; tool calls should be stored in `tool_calls` where applicable.
- Tool results use role `tool`, `tool_call_id`, `name`, and may include rich UI data in `metadata.contentBlocks`.
- Message order is stable and chronological within the returned page.
- SSE `assistant.message` and persisted final messages represent the same assistant turn; clients should avoid duplicating a streamed bubble and a final persisted message.

## Stream completion semantics

Terminal events are `run.succeeded`, `run.failed`, `run.cancelled`, and `run.timed_out`. `run.suspended` and `client.action.required` are not terminal backend states, but they are stream-completion states for mobile UI: clients should leave loading state and render a waiting/action state.

## Client/library boundaries

Reusable libraries own event parsing, stream lifecycle/reconnect helpers, reducer/state machine behavior, generic tool-call and required-action state, unknown-event tolerance, and canonical fixtures/tests.

Host products own org/workspace/channel navigation, product-specific timelines, push notifications, terminal sessions, integration-specific UI, branding, and app-specific persistence.
