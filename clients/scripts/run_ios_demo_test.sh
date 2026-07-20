#!/usr/bin/env bash
# Run the long visual `testDemoBigConversation` XCUITest. Drives the
# stub server with the demo_big_conversation fixture: 52 events,
# multi-agent handoffs, content blocks, ~12 s of streamed output. Meant
# to be watched, not gated on in CI.
#
# Assumes `start_stub_server.sh` is already running in another shell.
#
# Env overrides:
#   STUB_SERVER_URL  default http://127.0.0.1:8765
#   SIMULATOR        default "iPhone 17"
#   SCHEME           default AgentExample
#   TEST_NAME        default testDemoBigConversation (set to another
#                    ChatStreamingUITests case to run something else)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXAMPLE_DIR="$REPO_ROOT/clients/agent-ios/Example"

STUB_SERVER_URL="${STUB_SERVER_URL:-http://127.0.0.1:8765}"
SIMULATOR="${SIMULATOR:-iPhone 17}"
SCHEME="${SCHEME:-AgentExample}"
TEST_NAME="${TEST_NAME:-testDemoBigConversation}"

# Pre-flight the stub so we don't burn 30 s on a build only to time out.
if ! curl -sf -o /dev/null "$STUB_SERVER_URL/api/agent-runtime/health" \
        && ! curl -s -o /dev/null -w "%{http_code}" "$STUB_SERVER_URL/" | grep -qE "^[0-9]"; then
    echo "ERROR: stub server unreachable at $STUB_SERVER_URL" >&2
    echo "       Start it with: ./clients/scripts/start_stub_server.sh" >&2
    exit 1
fi

cd "$EXAMPLE_DIR"

if ! command -v xcodegen >/dev/null 2>&1; then
    echo "ERROR: xcodegen not found. Install with: brew install xcodegen" >&2
    exit 1
fi

# Regenerate so the project is consistent with project.yml. STUB_SERVER_URL
# is passed to the app via the test's launchEnvironment, not via scheme env.
echo "[run_ios_demo_test] regenerating Xcode project..."
xcodegen generate >/dev/null

if command -v xcbeautify >/dev/null 2>&1; then
    formatter=(xcbeautify)
else
    formatter=(grep -E "Test Case|Test Suite|Executed|passed|failed|error:|TEST ")
fi

# Pick the simulator UDID up front so we can boot it explicitly and the
# app window stays visible for the full ~12 s demo run.
udid="$(xcrun simctl list devices available -j 2>/dev/null \
    | python3 -c "
import json, sys
data=json.load(sys.stdin)
for runtime, devices in data['devices'].items():
    if 'iOS' not in runtime: continue
    for d in devices:
        if d.get('isAvailable') and d['name'] == \"$SIMULATOR\":
            print(d['udid']); sys.exit(0)
" || true)"

if [[ -n "$udid" ]]; then
    state="$(xcrun simctl list devices -j | python3 -c "
import json, sys
data=json.load(sys.stdin)
for devs in data['devices'].values():
    for d in devs:
        if d['udid'] == \"$udid\":
            print(d['state']); sys.exit(0)
")"
    if [[ "$state" != "Booted" ]]; then
        echo "[run_ios_demo_test] booting $SIMULATOR ($udid)..."
        xcrun simctl boot "$udid" 2>/dev/null || true
    fi
    open -a Simulator --args -CurrentDeviceUDID "$udid" 2>/dev/null || true
fi

echo "[run_ios_demo_test] running $TEST_NAME on \"$SIMULATOR\"..."
echo "[run_ios_demo_test] tip: bring the Simulator window to the front to watch."
set -o pipefail
xcodebuild \
    -project AgentExample.xcodeproj \
    -scheme "$SCHEME" \
    -destination "platform=iOS Simulator,name=$SIMULATOR" \
    -only-testing:AgentExampleUITests/ChatStreamingUITests/"$TEST_NAME" \
    test \
    | "${formatter[@]}"
