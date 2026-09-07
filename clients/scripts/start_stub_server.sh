#!/usr/bin/env bash
# Start the Python SSE stub server that replays canned event fixtures.
# Used by all of the fast Level-C UI tests, including the long visual
# `testDemoBigConversation` / `demoBigConversation` demo.
#
# Env overrides:
#   STUB_PORT   default 8765
#   PYTHON      Python executable (otherwise use the stub's .venv or python3)
#   AGENT_VENV  optional virtualenv to use instead of the stub's .venv

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STUB_DIR="$REPO_ROOT/test-harness/stub-server"
STUB_PORT="${STUB_PORT:-8765}"
if [[ -z "${PYTHON:-}" ]]; then
    if [[ -n "${AGENT_VENV:-}" ]]; then
        PYTHON="$AGENT_VENV/bin/python"
    elif [[ -x "$STUB_DIR/.venv/bin/python" ]]; then
        PYTHON="$STUB_DIR/.venv/bin/python"
    else
        PYTHON=python3
    fi
fi

if ! "$PYTHON" -c "import flask, requests" >/dev/null 2>&1; then
    echo "ERROR: stub dependencies are unavailable in the selected Python." >&2
    echo "Create test-harness/stub-server/.venv and install its requirements.txt first." >&2
    exit 1
fi

if command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$STUB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $STUB_PORT is already in use; stop the existing process first." >&2
    exit 1
fi

cd "$STUB_DIR"

echo "[start_stub_server] serving fixtures from $STUB_DIR on :$STUB_PORT"
echo "[start_stub_server] Ctrl-C to stop."
exec "$PYTHON" server.py --port "$STUB_PORT" "$@"
