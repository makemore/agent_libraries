## SSE fixtures

Recorded sequences of Server-Sent Events that the agent backend
(`django_agent_runtime`) emits during a run. Each fixture is a single
JSON file consumed by:

- the Python stub server (`test-harness/stub-server/`) — replays the
  fixture with timing for instrumented UI tests;
- the iOS Level-A tests (`AgentClientTests`) — fed through a
  `URLProtocol` mock so the real `SSEClient` and `ChatViewModel` are
  exercised end-to-end without a network;
- the Android Level-A tests (`agentfrontend` JVM `test` source set) —
  fed through `okhttp3.mockwebserver` for the same purpose.

### Fixture format

```json
{
  "name": "simple-streaming",
  "description": "A short streamed assistant reply followed by run.succeeded.",
  "run_id": "test-run-001",
  "conversation_id": "test-conv-001",
  "events": [
    { "delay_ms": 30, "event": "assistant.delta",   "payload": { "delta": "Hello" } },
    { "delay_ms": 30, "event": "assistant.delta",   "payload": { "delta": " world" } },
    { "delay_ms": 30, "event": "assistant.message", "payload": { "content": "Hello world", "role": "assistant" } },
    { "delay_ms":  0, "event": "run.succeeded",     "payload": {} }
  ]
}
```

`delay_ms` is the wait *before* sending the event, used by the stub
server to mimic real provider pacing. The Level-A mock transports
emit all events immediately so tests stay deterministic; they assert the
client's settled state rather than wall-clock playback timing.

### Wire format produced

Each event is serialised as the same SSE frame the real backend
produces, see `agent/django_agent_runtime/api/views.py::event_generator`:

```
event: assistant.delta
data: {"run_id":"<id>","seq":<n>,"type":"assistant.delta","payload":{"delta":"Hello"},"ts":"<iso>","visibility_level":"public","ui_visible":true}

```

### Fixtures included

| File | What it exercises |
|---|---|
| `simple_final_message.json` | Non-streaming user turn → assistant final message → success. |
| `simple_streaming.json` | Plain text streaming deltas → final message → success. |
| `tool_call_with_content_blocks.json` | Streamed prelude → tool.call → tool.result → content.blocks (card + table) → final message → success. |
| `tool_call_failure.json` | Tool call result contains an error payload while the run recovers. |
| `sai_multi_agent_handoff.json` | S'Ai-style: parent stream → sub_agent.start (specialist) → sub-agent deltas → sub-agent message → sub_agent.end → parent echo (suppressed) → parent extension → final message → success. |
| `sai_multi_agent_with_blocks.json` | S'Ai-style multi-agent run that also emits content blocks (callout + actionButtons + cardList) from a sub-agent's tool. |
| `run_failed.json` | Partial stream interrupted by `run.failed`. |
| `cancelled_mid_stream.json` | Long stream terminated by `run.cancelled`. |
| `required_action.json` | Streamed prelude followed by generic `client.action.required` for host-rendered action cards. |
| `required_action_lifecycle.json` | Required action requested/submitted/resolved markers plus resume. |
| `duplicate_replayed_event.json` | Duplicate replay of the same `run_id` + `seq` for reducer dedupe tests. |
| `malformed_unknown_event.json` | Unknown named event plus malformed raw frame samples for parser tests. |
| `reconnect_resume_from_seq.json` | Sequence-bearing stream suitable for `from_seq` reconnect/resume tests. |
| `supersede_retry.json` | Edit/retry marker documented as a backend gap: no standard supersede event yet. |
