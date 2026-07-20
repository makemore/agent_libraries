#!/usr/bin/env bash
# Start the Python SSE stub server that replays canned event fixtures.
# Used by all of the fast Level-C UI tests, including the long visual
# `testDemoBigConversation` / `demoBigConversation` demo.
#
# Env overrides:
#   STUB_PORT   default 8765
#   AGENT_VENV  default ~/.virtualenvs/agent_studio (only used to find a
#               python with `flask` installed; pass any venv that has it).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STUB_DIR="$REPO_ROOT/clients/test-stub-server"
STUB_PORT="${STUB_PORT:-8765}"
AGENT_VENV="${AGENT_VENV:-$HOME/.virtualenvs/agent_studio}"

# Pick a python: prefer the venv if it exists, otherwise fall back to system.
if [[ -f "$AGENT_VENV/bin/activate" ]]; then
    # shellcheck disable=SC1090
    source "$AGENT_VENV/bin/activate"
fi

if ! python -c "import flask" >/dev/null 2>&1; then
    echo "[start_stub_server] installing stub deps into $(python -c 'import sys; print(sys.prefix)')..."
    pip install -q -r "$STUB_DIR/requirements.txt"
fi

if lsof -nP -iTCP:"$STUB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $STUB_PORT is already in use; stop the existing process first." >&2
    lsof -nP -iTCP:"$STUB_PORT" -sTCP:LISTEN >&2 || true
    exit 1
fi

cd "$STUB_DIR"

cleanup() {
    echo
    echo "[start_stub_server] shutting down..."
    jobs -p | xargs -r kill 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[start_stub_server] serving fixtures from $STUB_DIR on :$STUB_PORT"
echo "[start_stub_server] Ctrl-C to stop."
exec python server.py --port "$STUB_PORT"
