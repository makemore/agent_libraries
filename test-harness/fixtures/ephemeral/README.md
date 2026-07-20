# Ephemeral parity contract

`contract.json` is the **shared oracle** for ephemeral-mode behaviour. Both
mobile clients drive the same scenarios from it and assert the same outcomes,
so the two platforms cannot silently diverge on the privacy-critical path:

- iOS: `agent-ios/Tests/AgentClientTests/EphemeralContractParityTests.swift`
- Android: `agent-android/src/test/java/com/makemore/agentfrontend/streaming/EphemeralContractParityTest.kt`

Each scenario pins, per turn:

1. `ephemeral: true` is present on every `POST /runs`.
2. The **full local history is re-sent** each turn (turn *N* carries `2N-1`
   messages) — the server reconstructs nothing from its own DB.
3. **No `conversationId` is sent on turn 1**; subsequent turns reuse the
   server-returned id (`serverConversationId`).
4. The client **never fetches history** from the server (`forbiddenPathSubstring`).

The SSE responses are replayed from the sibling `../sse/<fixture>.json`
fixtures, so request *and* response bytes are identical across platforms.

**Editing this file is a cross-platform change.** Both the iOS and Android
parity tests load it; a CI change here must keep both suites green. See
`agent/docs/ephemeral-security-validation-plan.md` (Layer B).
