## Streaming UI test stub server

A small Flask server that pretends to be `django_agent_runtime` for the
purpose of mobile-client UI tests. It replays the JSON fixtures in
`test-harness/fixtures/sse/` as real Server-Sent Event streams, with
configurable per-event delays, so the iOS/Android example apps can be
exercised end-to-end against deterministic input.

### Endpoints

| Method | Path                                                  | Behaviour                                                                                    |
|--------|-------------------------------------------------------|----------------------------------------------------------------------------------------------|
| POST   | `/api/accounts/anonymous-session/`                    | Returns `{"token": "stub-anonymous-token"}`.                                                 |
| POST   | `/api/agent-runtime/runs/`                            | Picks a fixture (see below), records it under a fresh `run_id`, returns the run + ids.       |
| GET    | `/api/agent-runtime/runs/<run_id>/stream/`            | Replays the recorded fixture as SSE (iOS default endpoint, mirrors the real async route).    |
| GET    | `/api/agent-runtime/runs/<run_id>/events/`            | Same as `/stream/` (Android default endpoint, mirrors the real sync polling route).          |
| POST   | `/api/agent-runtime/runs/<run_id>/cancel/`            | No-op `204`.                                                                                 |
| GET    | `/api/agent-runtime/conversations/`                   | Returns `{"results": []}`.                                                                   |
| GET    | `/api/agent-runtime/conversations/<id>/`              | Returns an empty conversation.                                                                |
| GET    | `/health`                                             | `{"ok": true, "fixtures": [...]}` for liveness.                                              |

### How fixture selection works

The fixture replayed on the SSE endpoint is the one that was associated
with the run when it was created. `POST /runs/` looks for the fixture
name in this order:

1. The `X-Test-Fixture` request header.
2. The `?fixture=` query parameter.
3. The `metadata.test_fixture` field of the JSON body.
4. Falls back to `simple_streaming`.

This lets the iOS XCUITest / Android instrumented test pick a scenario
per launch (env var → header) without needing a different endpoint per
fixture.

### Running locally

```
cd test-harness/stub-server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py --port 8001
```

Then point a client at `http://localhost:8001`.

From the meta-repo root, `bash clients/scripts/start_stub_server.sh` uses
the stub's `.venv` when present and defaults to port 8765. Override the
interpreter with `PYTHON` or a virtualenv with `AGENT_VENV`; set `STUB_PORT`
to change the port. The launcher never installs dependencies automatically.
Extra arguments such as `--no-delay` are forwarded to the server.

Run offline regression tests with `make test-harness` from the meta-repo
root, setting `PYTHON` to the interpreter with these requirements installed.

To run with no per-event delay (tests):

```
python server.py --port 8001 --no-delay
```

### Passthrough mode

Pass `--passthrough https://real.backend.example.com` to forward every
request to a real `agent_studio` instance instead of replaying fixtures
— useful when you want to debug an iOS/Android UI session against the
genuine backend through the same example app build:

```
python server.py --port 8001 --passthrough http://127.0.0.1:8000
```
