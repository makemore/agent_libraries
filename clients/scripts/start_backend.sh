#!/usr/bin/env bash
# Start the Django agent_studio dev server + runagent worker so the iOS /
# Android real-backend UI tests have something to talk to. Foreground; Ctrl-C
# tears both processes down.
#
# Env overrides:
#   AGENT_VENV  path to the virtualenv to activate (default ~/.virtualenvs/agent_studio)
#   PORT        runserver port (default 8000)
#   SETTINGS    Django settings module (default agent_studio.settings.dev)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent/agent_studio"
AGENT_VENV="${AGENT_VENV:-$HOME/.virtualenvs/agent_studio}"
PORT="${PORT:-8000}"
SETTINGS="${SETTINGS:-agent_studio.settings.dev}"

if [[ ! -d "$AGENT_DIR" ]]; then
    echo "ERROR: agent_studio not found at $AGENT_DIR" >&2
    exit 1
fi

if [[ ! -f "$AGENT_VENV/bin/activate" ]]; then
    echo "ERROR: virtualenv not found at $AGENT_VENV" >&2
    echo "       Set AGENT_VENV=/path/to/venv if it lives elsewhere." >&2
    exit 1
fi

# shellcheck disable=SC1090
source "$AGENT_VENV/bin/activate"

export DJANGO_SETTINGS_MODULE="$SETTINGS"
export PYTHONUNBUFFERED=1

cd "$AGENT_DIR"

# Bail early if port is already taken so we don't leave a half-started stack.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: port $PORT is already in use; stop the existing process first." >&2
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
    exit 1
fi

pids=()
cleanup() {
    echo
    echo "[start_backend] shutting down..."
    for pid in "${pids[@]:-}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[start_backend] runagent worker..."
python manage.py runagent &
pids+=("$!")

echo "[start_backend] runserver on :$PORT ..."
python manage.py runserver "$PORT" &
pids+=("$!")

echo "[start_backend] both processes up. Ctrl-C to stop."
wait -n
# If one dies, kill the other and exit with its status.
exit $?
