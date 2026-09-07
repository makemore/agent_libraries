"""Stub Server-Sent-Events backend for iOS/Android streaming UI tests.

Replays the JSON fixtures in ``test-harness/fixtures/sse/`` over the same
URL shape that ``django_agent_runtime`` exposes, so that the example apps
can talk to it without any modification.

See the sibling README for endpoint details.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from flask import Flask, Response, jsonify, request, stream_with_context


HERE = Path(__file__).resolve().parent
FIXTURES_DIR = (HERE.parent / "fixtures" / "sse").resolve()
DEFAULT_FIXTURE = "simple_streaming"


# -- fixture loading ---------------------------------------------------------


def load_fixtures() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid fixture {path}: {exc}") from exc
        name = data.get("name") or path.stem
        out[name] = data
    if not out:
        raise SystemExit(f"No fixtures found in {FIXTURES_DIR}")
    return out


# -- app factory -------------------------------------------------------------


def create_app(*, no_delay: bool = False, passthrough: str | None = None) -> Flask:
    app = Flask(__name__)
    fixtures = load_fixtures()
    runs: dict[str, dict[str, Any]] = {}
    runs_lock = threading.Lock()
    # Per-conversation chain pointer. When a fixture defines `next_fixture`,
    # the next `create_run` against the same conversation_id is served that
    # fixture instead of falling back to the request's metadata. Lets the
    # demo open with the long fixture and then run short follow-up turns.
    convo_to_next_fixture: dict[str, str] = {}
    convo_lock = threading.Lock()

    # ---- helpers -----------------------------------------------------------

    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def pick_fixture(req) -> dict[str, Any]:
        name = req.headers.get("X-Test-Fixture") \
            or req.args.get("fixture") \
            or (req.get_json(silent=True) or {}).get("metadata", {}).get("test_fixture") \
            or DEFAULT_FIXTURE
        fx = fixtures.get(name)
        if fx is None:
            raise ValueError(f"Unknown fixture {name!r} (available: {sorted(fixtures)})")
        return fx

    def sse_frames(fixture: dict[str, Any], run_id: str) -> Iterable[bytes]:
        seq = 0
        for event in fixture.get("events", []):
            delay = 0.0 if no_delay else max(0, int(event.get("delay_ms", 0))) / 1000.0
            if delay:
                time.sleep(delay)
            event_type = event["event"]
            event_seq = int(event.get("seq_override", seq))
            data = {
                "run_id": run_id,
                "seq": event_seq,
                "type": event_type,
                "payload": event.get("payload", {}),
                "ts": now_iso(),
                "visibility_level": "user",
                "ui_visible": True,
            }
            seq += 1
            yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()

    # ---- proxy -------------------------------------------------------------

    def proxy(path: str) -> Response:
        url = passthrough.rstrip("/") + "/" + path.lstrip("/")
        upstream = requests.request(
            method=request.method,
            url=url,
            params=request.args,
            data=request.get_data(),
            headers={k: v for k, v in request.headers if k.lower() != "host"},
            stream=True,
            timeout=300,
        )
        excluded = {"content-encoding", "transfer-encoding", "connection"}
        headers = [(k, v) for k, v in upstream.raw.headers.items() if k.lower() not in excluded]
        return Response(upstream.iter_content(chunk_size=512), status=upstream.status_code, headers=headers)

    # ---- routes ------------------------------------------------------------

    @app.get("/health")
    def health():  # noqa: D401
        return jsonify({"ok": True, "fixtures": sorted(fixtures), "passthrough": passthrough})

    @app.post("/api/accounts/anonymous-session/")
    def anonymous_session():
        if passthrough:
            return proxy("api/accounts/anonymous-session/")
        return jsonify({"token": "stub-anonymous-token"})

    @app.get("/api/agent-runtime/conversations/")
    def list_conversations():
        if passthrough:
            return proxy("api/agent-runtime/conversations/")
        return jsonify({"results": [], "count": 0})

    @app.get("/api/agent-runtime/conversations/<conv_id>/")
    def get_conversation(conv_id: str):
        if passthrough:
            return proxy(f"api/agent-runtime/conversations/{conv_id}/")
        return jsonify({"id": conv_id, "messages": [], "title": "Stub conversation"})

    @app.post("/api/agent-runtime/runs/")
    def create_run():
        if passthrough:
            return proxy("api/agent-runtime/runs/")
        body = request.get_json(silent=True) or {}
        # Accept both camelCase (current standard) and snake_case (legacy) so
        # the stub mirrors the real backend's parser, which is tolerant.
        client_conv = body.get("conversationId") or body.get("conversation_id")
        with convo_lock:
            chained = convo_to_next_fixture.get(client_conv) if client_conv else None
        if chained and chained in fixtures:
            fixture = fixtures[chained]
        else:
            try:
                fixture = pick_fixture(request)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        # Always mint a fresh run_id — the same fixture may be served for
        # multiple turns in a chained conversation.
        run_id = str(uuid.uuid4())
        conversation_id = client_conv or fixture.get("conversation_id") or str(uuid.uuid4())
        with runs_lock:
            runs[run_id] = {"fixture": fixture, "conversation_id": conversation_id}
        next_fix = fixture.get("next_fixture")
        with convo_lock:
            if next_fix:
                convo_to_next_fixture[conversation_id] = next_fix
            else:
                convo_to_next_fixture.pop(conversation_id, None)
        return jsonify({
            "id": run_id,
            "conversationId": conversation_id,
            "status": "running",
            "createdAt": now_iso(),
        })

    @app.post("/api/agent-runtime/runs/<run_id>/cancel/")
    def cancel_run(run_id: str):
        if passthrough:
            return proxy(f"api/agent-runtime/runs/{run_id}/cancel/")
        return ("", 204)

    def _stream_response(run_id: str, suffix: str) -> Response:
        if passthrough:
            return proxy(f"api/agent-runtime/runs/{run_id}/{suffix}/")
        with runs_lock:
            entry = runs.get(run_id)
        if entry is None:
            try:
                fixture = pick_fixture(request)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 404
        else:
            fixture = entry["fixture"]
        return Response(
            stream_with_context(sse_frames(fixture, run_id)),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Mirror both routes the real backend exposes — iOS uses `/stream/`
    # (async/in-memory) and Android uses `/events/` (sync/postgres-backed).
    @app.get("/api/agent-runtime/runs/<run_id>/stream/")
    def stream_run(run_id: str):
        return _stream_response(run_id, "stream")

    @app.get("/api/agent-runtime/runs/<run_id>/events/")
    def events_run(run_id: str):
        return _stream_response(run_id, "events")

    return app


# -- CLI ---------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--no-delay", action="store_true", help="Ignore per-event delay_ms.")
    parser.add_argument("--passthrough", default=None, help="Proxy all requests to this base URL.")
    args = parser.parse_args()
    app = create_app(no_delay=args.no_delay, passthrough=args.passthrough)
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
