#!/usr/bin/env bash
# Run the iOS XCUITest that exercises ChatWidgetView against the real Django
# backend. Assumes `start_backend.sh` is already running in another shell.
#
# Env overrides:
#   BACKEND_URL  default http://localhost:8000
#   AGENT_TOKEN  required (DRF token for a Django user)
#   AGENT_KEY    default agent-builder
#   SIMULATOR    default "iPhone 17"
#   SCHEME       default AgentExample

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXAMPLE_DIR="$REPO_ROOT/clients/agent-ios/Example"

export BACKEND_URL="${BACKEND_URL:-http://localhost:8000}"
export AGENT_TOKEN="${AGENT_TOKEN:-}"
export AGENT_KEY="${AGENT_KEY:-agent-builder}"
SIMULATOR="${SIMULATOR:-iPhone 17}"
SCHEME="${SCHEME:-AgentExample}"

if [[ -z "$AGENT_TOKEN" ]]; then
    echo "ERROR: AGENT_TOKEN env var is required (DRF token for a Django user)." >&2
    echo "       Generate one with:" >&2
    echo "         python manage.py shell -c \\" >&2
    echo "           \"from rest_framework.authtoken.models import Token;\\" >&2
    echo "            from django.contrib.auth import get_user_model;\\" >&2
    echo "            u=get_user_model().objects.filter(is_superuser=True).first();\\" >&2
    echo "            print(Token.objects.get_or_create(user=u)[0].key)\"" >&2
    exit 1
fi

# Confirm the backend is actually reachable before we burn 30 s on a build.
if ! curl -sf -o /dev/null -H "Authorization: Token $AGENT_TOKEN" \
        "$BACKEND_URL/api/agent-runtime/agents/"; then
    echo "ERROR: cannot reach $BACKEND_URL/api/agent-runtime/agents/ with the supplied token." >&2
    echo "       Is start_backend.sh running? Is the token valid?" >&2
    exit 1
fi

cd "$EXAMPLE_DIR"

if ! command -v xcodegen >/dev/null 2>&1; then
    echo "ERROR: xcodegen not found. Install with: brew install xcodegen" >&2
    exit 1
fi

# Regenerate so BACKEND_URL / AGENT_TOKEN get baked into the scheme's
# environmentVariables (xcodegen substitutes ${VAR} from the shell env).
echo "[run_ios_real_backend_test] regenerating Xcode project..."
xcodegen generate >/dev/null

if command -v xcbeautify >/dev/null 2>&1; then
    formatter=(xcbeautify)
else
    formatter=(grep -E "Test Case|Test Suite|Executed|passed|failed|error:|TEST ")
fi

echo "[run_ios_real_backend_test] running test on \"$SIMULATOR\"..."
set -o pipefail
xcodebuild \
    -project AgentExample.xcodeproj \
    -scheme "$SCHEME" \
    -destination "platform=iOS Simulator,name=$SIMULATOR" \
    -only-testing:AgentExampleUITests/ChatStreamingUITests/testRealBackendAgentBuilderReplies \
    test \
    | "${formatter[@]}"
