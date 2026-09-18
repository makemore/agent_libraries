# Streaming UI tests for the mobile clients

End-to-end test coverage for the SSE streaming code paths in
`clients/agent-ios` and `clients/agent-android`. Two layers, both driven
by the same JSON fixtures so iOS and Android assert on identical
sequences of events:

| Layer | What it does | Where |
|---|---|---|
| **Level A** | Drives the real `ChatViewModel` + `APIClient` + `SSEClient` against an in-process HTTP mock that replays the fixtures. No emulator/simulator. Fastest, deterministic, runs in CI per PR. | iOS: `Tests/AgentFrontendTests/Streaming/`<br>Android: `src/test/java/.../streaming/` |
| **Level C** | Builds and launches a host UI on a real simulator/emulator pointed at a local Python stub server (`test-harness/stub-server/`) replaying the same fixtures over real SSE. Asserts on the rendered chat. | iOS: `Example/ExampleAppUITests/`<br>Android: `src/androidTest/java/.../streaming/` |

Both layers cover the same five scenarios:

| Fixture | Asserts |
|---|---|
| `simple_streaming` | Plain typewriter delta → finalised assistant bubble. |
| `tool_call_with_content_blocks` | Tool call → `content.blocks` decoded into a Card + Table bubble → final reply. |
| `sai_multi_agent_handoff` | S'Ai-style hand-off; parent intro, sub-agent reply, parent echo suppressed. |
| `sai_multi_agent_with_blocks` | Multi-agent flow with a Callout + CardList + ActionButtons block from a sub-agent tool. |
| `run_failed` | `run.failed` surfaces an error banner / `vm.error`. |

Fixtures live in [`test-harness/fixtures/sse/`](../test-harness/fixtures/sse/README.md).

## Running Level A (no device)

### iOS
```bash
cd clients/agent-ios
xcodebuild -scheme AgentFrontend \
  -destination "platform=iOS Simulator,name=iPhone 17" \
  -only-testing:AgentFrontendTests/SSEStreamingTests test
```

### Android
```bash
cd clients/agent-android
./gradlew :test --tests "com.makemore.agentfrontend.streaming.SSEStreamingTests"
```

## Running Level C (simulator / emulator)

### 1. Start the stub server
```bash
cd test-harness/stub-server
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py --port 8765
```
Health check: <http://127.0.0.1:8765/health>. See
[`test-harness/stub-server/README.md`](../test-harness/stub-server/README.md) for
the full protocol, header overrides and `--passthrough` mode.

### 2a. iOS
The host app lives at `clients/agent-ios/Example` and is generated with
[xcodegen](https://github.com/yonaskolb/XcodeGen):
```bash
cd clients/agent-ios/Example
xcodegen generate
xcodebuild -project AgentExample.xcodeproj -scheme AgentExample \
  -destination "platform=iOS Simulator,name=iPhone 17" \
  -only-testing:AgentExampleUITests/ChatStreamingUITests test
```
The XCUITest passes `STUB_SERVER_URL`, `TEST_FIXTURE`, `AUTO_SEND` and
`AUTO_SEND_PROMPT` via `XCUIApplication.launchEnvironment`; the host
app forwards `TEST_FIXTURE` to the stub through the unmodified
`metadata` field of the create-run request.

### 2b. Android
Boot any emulator (API 34+ — see note below) and run:
```bash
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd clients/agent-android
./gradlew :connectedDebugAndroidTest
```
The instrumentation test reads `STUB_SERVER_URL` from
`testInstrumentationRunnerArguments` (default `http://10.0.2.2:8765`,
the emulator's loopback to the host machine). Override per-run with:
```bash
./gradlew :connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.STUB_SERVER_URL=http://10.0.2.2:9000
```

> **API 35+ note**: `androidx.test.espresso:espresso-core` < 3.7.0
> reflects on `InputManager.getInstance()`, which was removed in
> Android 14+. The Gradle build pins `espresso-core:3.7.0`,
> `runner:1.7.0` and `ext:junit:1.3.0` to keep Compose UI tests working
> on modern emulators; do not downgrade these.

The library's `src/debug/AndroidManifest.xml` enables
`usesCleartextTraffic` so the test APK can talk to the stub over plain
HTTP. This is debug-only and is not merged into release builds.

## Running against a real backend

For manual debugging the host apps and the stub server can both point
at a real `agent_studio` instance instead of a fixture.

- **Stub passthrough**: `python server.py --passthrough http://127.0.0.1:8000`
  forwards every request unchanged. The Level C suites then exercise
  the production backend through the production client code paths.
- **Direct host-app launch (iOS)**: open the generated `AgentExample`
  Xcode project, edit the scheme's environment to set
  `STUB_SERVER_URL=http://127.0.0.1:8000` (or your real hostname) and
  `AGENT_KEY=<your agent>`, and run.
- **Direct host-app launch (Android)**: there is no separate sample
  module; run the instrumentation test with
  `-Pandroid.testInstrumentationRunnerArguments.STUB_SERVER_URL=http://10.0.2.2:8000`.

## Continuous integration

Both layers are intended to run in CI:

- Level A on every PR (no device needed) — ~15 s per platform.
- Level C nightly or on `main` — boots an emulator/simulator, starts the
  Python stub, runs the suite, tears everything down. The stub is
  hermetic: there are no network or backend dependencies.
