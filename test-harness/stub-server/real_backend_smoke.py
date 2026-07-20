#!/usr/bin/env python3
"""
End-to-end smoke test against the real Django agent_studio backend.

Posts to /api/agent-runtime/runs/ then streams /runs/<id>/events/ via SSE,
prints every event, and exits 0 only when run.succeeded is observed.

Usage:
    BACKEND_URL=http://localhost:8000 \
    AGENT_TOKEN=<token> \
    AGENT_KEY=agent-builder \
    python real_backend_smoke.py "Say hi in five words."
"""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import urljoin

import requests


BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("AGENT_TOKEN", "")
AGENT_KEY = os.environ.get("AGENT_KEY", "agent-builder")
TIMEOUT_S = int(os.environ.get("TIMEOUT_S", "120"))

if not TOKEN:
    sys.exit("AGENT_TOKEN env var is required")

PROMPT = " ".join(sys.argv[1:]) or "Say hi in five words."

HEADERS = {
    "Authorization": f"Token {TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def create_run() -> str:
    url = urljoin(BACKEND + "/", "api/agent-runtime/runs/")
    body = {
        "agentKey": AGENT_KEY,
        "messages": [{"role": "user", "content": PROMPT}],
    }
    print(f"POST {url}\n  body={json.dumps(body)}")
    r = requests.post(url, json=body, headers=HEADERS, timeout=10)
    print(f"  -> HTTP {r.status_code}")
    if r.status_code >= 300:
        print("  body:", r.text[:500])
        r.raise_for_status()
    data = r.json()
    run_id = data.get("id") or data.get("runId")
    print(f"  run_id={run_id} status={data.get('status')}")
    return run_id


def stream_events(run_id: str) -> bool:
    # The /events/ view is a plain Django view (not DRF), so it ignores the
    # Authorization header. It supports ?token=<key> for token auth instead.
    url = urljoin(BACKEND + "/", f"api/agent-runtime/runs/{run_id}/events/?token={TOKEN}")
    sse_headers = {**HEADERS, "Accept": "text/event-stream"}
    sse_headers.pop("Content-Type", None)
    print(f"\nGET {url} (SSE)")
    started = time.time()
    saw_succeeded = False
    saw_failed = False
    with requests.get(url, headers=sse_headers, stream=True, timeout=TIMEOUT_S) as r:
        print(f"  -> HTTP {r.status_code} content-type={r.headers.get('Content-Type')}")
        if r.status_code != 200:
            print("  body:", r.text[:500])
            return False
        event_name = None
        for raw in r.iter_lines(decode_unicode=True):
            if time.time() - started > TIMEOUT_S:
                print("  timeout reached")
                return False
            if raw is None:
                continue
            line = raw.rstrip("\r")
            if line == "":
                event_name = None
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                payload = line[5:].lstrip()
                try:
                    parsed = json.loads(payload)
                except Exception:
                    parsed = payload
                evt = event_name or (parsed.get("event") if isinstance(parsed, dict) else None) or "?"
                summary = _summarize(evt, parsed)
                print(f"  · {evt:<22} {summary}")
                if evt in ("run.succeeded", "run_succeeded"):
                    saw_succeeded = True
                    return True
                if evt in ("run.failed", "run_failed"):
                    saw_failed = True
                    return False
    return saw_succeeded and not saw_failed


def _summarize(evt: str, data) -> str:
    if not isinstance(data, dict):
        return str(data)[:120]
    if evt.endswith("delta"):
        return repr(data.get("delta") or data.get("text") or "")[:120]
    if evt.endswith("message"):
        msg = data.get("message") or data
        content = msg.get("content") if isinstance(msg, dict) else None
        return repr(content)[:120] if content else json.dumps(msg)[:120]
    keys = sorted(data.keys())
    return f"keys={keys}"


def main() -> int:
    run_id = create_run()
    ok = stream_events(run_id)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
