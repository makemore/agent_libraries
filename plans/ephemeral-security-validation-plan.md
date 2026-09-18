# Ephemeral Mode — Security Validation & Cross-Platform Test Plan

> **Goal.** Prove, with automated tests, that the ephemeral conversation mode does
> what we tell users it does — *the server does not retain conversation history* —
> and that our own code (clients + backend, **not** modal.com itself) handles the
> Modal model connection securely. Guarantee the behavior is **identical on iOS and
> Android**.
>
> **Companion doc:** [`ephemeral-conversations-plan.md`](./ephemeral-conversations-plan.md) (design).
> This doc is the validation layer on top of it.

---

## Implementation status (landed 2026-06-19)

| Item | Status | Where |
|------|--------|-------|
| §2 hard-delete + `EPHEMERAL_HARD_DELETE` / window=0 | ✅ done | `management/commands/cleanup_ephemeral.py`, `conf.py` |
| Layer A — server invariants + canary scan (15 tests) | ✅ green | `tests/test_ephemeral_invariants.py` |
| Layer D — Modal egress (8 tests) | ✅ green | `tests/test_modal_egress.py` |
| Layer B — shared contract + iOS parity | ✅ green | `test-fixtures/ephemeral/contract.json`, `agent-ios/Tests/.../EphemeralContractParityTests.swift` |
| Layer B — Android parity | ✅ green | `agent-android/.../streaming/EphemeralContractParityTest.kt` |
| Layer C — transport/log-hygiene guards (iOS+Android) | ✅ green | `ClientSecurityTests.swift`, `security/ClientSecurityTest.kt` |
| Layer C — Keychain / EncryptedSharedPreferences / file protection / https-enforcement | ⏳ backlog | encoded as documented `XCTSkip` / `@Ignore` |
| **Egress sovereignty** — per-run `private_only` fail-closed routing to the private (Modal) endpoint + client pass-through | ✅ green | core `llm/sovereignty.py`, `runtime/sovereignty.py`, runner contextvar, `tests/test_sovereignty.py`, iOS/Android `privateOnly` + parity scenario |
| **On-device encryption (P0, device capture)** — secrets→Keychain (iOS) / Keystore AES-GCM (Android), iOS DB file protection, clear-on-logout | ✅ green | iOS `Services/SecureStorage.swift` + `LocalHistoryStore.protectFileAtRest` + `clearAllLocalData()`; Android `services/SecureStorage.kt` + `clearAllLocalData()`; routing tests both platforms |
| On-device — Android conversation **DB** encryption (SQLCipher) | ⏳ follow-up | OS-FBE-protected today; SQLCipher adds native libs to the published AAR (needs device/ABI testing) — `@Ignore` placeholder |
| **Transport hardening (P0)** — fail-closed HTTPS enforcement (dev-host exemption + `allowInsecureHTTP` escape hatch) | ✅ green | iOS `APIClient.validateTransport`/`isDevHost` + `APIError.insecureTransport`; Android `APIClient.validateTransport`/`isDevHost` + `InsecureTransport`; tests both platforms |
| **Shipping-app config audit** — hardening checklist for the consuming app | ✅ done | `clients/SECURITY_HARDENING.md` (ATS/cleartext/allowBackup, pinning, logout-wipe, cron assurance) |
| Transport — certificate pinning | ⏳ follow-up | configure in host-app session today; library hook is a fast-follow (see checklist) |

### Egress sovereignty (added for UK MoD delivery)

A run is `private_only` when the consuming app sets it per-request (iOS/Android
`ChatWidgetConfig.privateOnly`, web/API `private_only`). The Django runner installs
a `SovereignPolicy` in a contextvar for that run; the universal LLM factory
(`agent_runtime_core.llm.get_llm_client`, plus the Django factory for title-gen)
enforces it **fail-closed**:

- pins every client to `PRIVATE_BASE_URL` (Modal);
- blocks the Anthropic provider (can't be pinned) and any provider ≠ `PRIVATE_PROVIDER`;
- rejects models outside `PRIVATE_MODELS` when that allowlist is set;
- **refuses to route at all** if `private_only` is set but no private endpoint is configured.

Concurrent public-mode runs on the same worker are unaffected (per-task contextvar).
This closes the `claude-*`→public-Anthropic data-egress footgun for restricted users.
Settings: `PRIVATE_BASE_URL`, `PRIVATE_PROVIDER`, `PRIVATE_MODELS` in `conf.py`.

Run: server `pytest tests/test_ephemeral_invariants.py tests/test_modal_egress.py`;
iOS `swift test --filter "EphemeralContractParityTests|ClientSecurityTests"`;
Android `./gradlew :testDebugUnitTest --tests "*EphemeralContractParityTest" --tests "*ClientSecurityTest"`.

Pre-existing, unrelated to this work: `agent-android` `SSEStreamingTests.multiAgentHandoffSuppressesParentEcho` fails on `main` (multi-agent fixture handling), and the server `test_openrouter.py` / `test_registry.py` shim errors come from stale site-packages copies of `agent_runtime_core` / `django_agent_studio`.

---

## 0. Current state (audited 2026-06-19)

The feature is **implemented end-to-end**, not just planned:

| Layer | Status | Key locations |
|-------|--------|---------------|
| Server | Implemented | `django_agent_runtime/api/views.py` (`create`, `pickup`), `runtime/runner.py` (`_load_conversation_history` ~365, `_finalize_run` ~580), `management/commands/cleanup_ephemeral.py`, `models/definitions.py` (`MessageStorageMode.EPHEMERAL`), `api/serializers.py` (`ephemeral` field) |
| iOS | Implemented | `ChatWidgetConfig.swift:123` (`ephemeral`), `ChatViewModel.swift` (`sendMessage` ~468, `restoreConversationIfNeeded` ~427), `APIClient+Requests.swift:109`, `Services/LocalHistoryStore.swift` (SQLite) |
| Android | Implemented | `ChatWidgetConfig.kt:113` (`ephemeral`), `ChatViewModel.kt` (`sendMessage` ~419, restore guard ~287), `networking/APIClientRequests.kt:102`, `agent-client/.../services/LocalHistoryStore.kt` (SQLite) |
| Shared test infra | Exists | `test-harness/fixtures/sse/*.json`, `test-harness/stub-server/` (Flask SSE replayer), `SSEFixture.swift` / `SSEFixture.kt` |

### Gaps this plan exists to close

1. **The privacy claim is currently overstated.** In ephemeral mode the server *does*
   write full message content to `AgentRun.input`/`output` and `AgentEvent.payload`
   for the pickup window (`EPHEMERAL_PICKUP_WINDOW_SECONDS`, default 24h), and
   `cleanup_ephemeral` **redacts (blanks) rather than hard-deletes**, and only runs if
   scheduled. **Decision: tighten to hard-delete** (see §2).
2. **iOS and Android already diverge.** Android guards against rehydrating a stale
   server `conversationId` and has a test (`ChatViewModelTest.kt`); iOS has the guard
   (`ChatViewModel.swift:427`) but **no test**. Token storage differs (iOS
   `UserDefaults` + token in SSE URL query; Android plain `SharedPreferences`).
3. **No DB-level proof** that content is gone after cleanup.
4. **No tests** that the Modal egress path is correctly and exclusively used, or that
   the Modal key is never logged/leaked.
5. **No security tests** for TLS enforcement, at-rest protection, or token storage.

---

## 1. What we are validating — claims & threat model

We validate four explicit claims. Every test maps to one.

| # | Claim | Layer | Adversary it defends against |
|---|-------|-------|------------------------------|
| **C1** | After the pickup window, **no conversation content remains anywhere on the server**. | Server (A) | Someone with DB/backup access after the fact; subpoena; breach. |
| **C2** | iOS and Android produce **byte-for-byte equivalent** ephemeral request bodies and identical local-store outcomes. | Clients (B) | Silent platform drift that breaks the privacy guarantee on one OS only. |
| **C3** | Client↔server transport and on-device storage are **confidential** (TLS-only, protected at rest, tokens not leaked). | Clients (C) | Network MITM; device theft / forensic extraction; cloud backup exfiltration. |
| **C4** | Inference traffic goes **only** to the configured Modal endpoint over TLS, and the Modal credential **never** leaks to logs, the DB, or the client. | Server (D) | Credential theft; accidental fallback to public OpenAI; data exfiltration via misrouting. |

Out of scope (explicitly): modal.com's own infrastructure, the LLM model weights/behavior, and Django/DRF framework internals.

---

## 2. Prerequisite code change — hard-delete (privacy target)

Tests in Layer A assert the **stricter** guarantee, so the code must move from
*redact* to *hard-delete* first.

**File: `django_agent_runtime/management/commands/cleanup_ephemeral.py`**

- Replace the field-blanking logic with row deletion:
  ```python
  expired = AgentRun.objects.filter(
      metadata__ephemeral=True,
      metadata__expires_at__lt=now.isoformat(),
  )
  run_ids = list(expired.values_list("id", flat=True))
  AgentEvent.objects.filter(run_id__in=run_ids).delete()   # was: .update(payload={})
  AgentFile.objects.filter(run_id__in=run_ids).delete()    # already hard-deletes
  deleted = expired.delete()[0]                            # was: blank input/output/error
  # Drop now-orphaned ephemeral conversations
  AgentConversation.objects.filter(
      metadata__ephemeral=True, runs__isnull=True,
  ).delete()
  ```
- Keep a tiny **non-content** audit row if analytics needs run counts (id, agent_key,
  timestamps, token counts — *never* message text).

**File: `django_agent_runtime/conf.py`**

- Allow `EPHEMERAL_PICKUP_WINDOW_SECONDS = 0` to mean "delete as soon as the run is
  terminal" (opt-out of the pickup window entirely for the strictest agents).
- Add `EPHEMERAL_HARD_DELETE: bool = True` so the behavior is explicit and testable.

**Scheduling is part of the guarantee.** Document and ship a default schedule
(supervisord/celery-beat) so cleanup is not optional. Add a startup check / health
endpoint flag that reports when cleanup last ran; Layer A asserts the command is
idempotent and safe to run frequently.

> If for any agent the pickup window must stay >0, the claim for *that* agent becomes
> "retained up to N seconds, then hard-deleted" — keep the window configurable per
> agent and surface it in the UI.

---

## 3. Layer A — Server invariant suite (source of truth for C1)

**Framework:** pytest + `pytest-django`, transactional DB.
**New file:** `packages/python/django_agent_runtime/tests/test_ephemeral_invariants.py`

### A1. Persistence discrimination
- `test_ephemeral_run_creates_no_normalized_messages` — run ephemeral; assert
  `Message.objects.count() == 0` and the conversation has no messages.
- `test_persistent_run_DOES_persist` — control: identical run without `ephemeral`
  persists normally. (Proves the test can tell the difference.)
- `test_ephemeral_skips_title_generation` and `test_ephemeral_skips_memory_persist`
  (with `EPHEMERAL_ALLOW_MEMORY_EXTRACTION=False`).

### A2. The canary test (strongest proof of C1)
- `test_no_content_survives_cleanup`:
  1. Embed a unique UUID canary string in the user message.
  2. Run the agent to terminal (stub LLM echoes / fixed output).
  3. Set the run's `expires_at` to the past; call `cleanup_ephemeral`.
  4. **Scan the entire DB**: iterate every table/`TextField`/`JSONField`, assert the
     canary substring appears in **zero** rows. Helper:
     ```python
     def assert_canary_absent(canary: str):
         for model in apps.get_models():
             for row in model.objects.all().values():
                 assert canary not in json.dumps(row, default=str), (model, row)
     ```
  5. Repeat with `EPHEMERAL_PICKUP_WINDOW_SECONDS=0` → canary absent immediately
     after the run is terminal (no window).

### A3. Pickup window boundary
- `test_pickup_returns_output_inside_window` → 200 + output.
- `test_pickup_returns_410_after_expiry`.
- `test_pickup_rejects_non_ephemeral_run` → 400.
- `test_pickup_requires_owner` → another user/anon gets 403/404 (authorization).

### A4. Limits & cleanup mechanics
- `test_max_ephemeral_messages_returns_413` (boundary at `MAX_EPHEMERAL_MESSAGES`).
- `test_cleanup_is_idempotent` — running twice deletes once, no error second time.
- `test_cleanup_only_touches_expired_ephemeral` — non-expired and persistent rows
  untouched.

### A5. Run safe-defaults
- `test_ephemeral_run_input_not_written_to_logs` — capture logging; assert message
  content not emitted at INFO/above.

---

## 4. Layer B — Cross-platform parity (source of truth for C2)

This is the core of the "consistent on iOS and Android" requirement. **One shared
spec drives both platforms.** If a platform diverges, its test fails.

### B1. The shared contract spec
**New file:** `test-harness/fixtures/ephemeral/contract.json`

```jsonc
{
  "scenarios": [
    {
      "name": "three_turn_history_resend",
      "agentKey": "test-agent",
      "turns": ["hello", "and again", "third"],
      "sseFixturePerTurn": ["simple_final_message", "demo_followup_1", "demo_followup_2"],
      "expectedRequests": [
        { "ephemeral": true, "conversationIdSent": false,
          "messages": [{"role":"user","content":"hello"}] },
        { "ephemeral": true, "conversationIdSent": false,
          "messages": [
            {"role":"user","content":"hello"},
            {"role":"assistant","content":"<from fixture 1>"},
            {"role":"user","content":"and again"}] },
        { "ephemeral": true, "conversationIdSent": false,
          "messages": [/* 5 messages: full history + 3rd user turn */] }
      ],
      "expectedLocalStore": {
        "conversationCount": 1,
        "messageRoles": ["user","assistant","user","assistant","user","assistant"]
      },
      "forbiddenCalls": ["GET conversations/", "GET conversations/{id}/"]
    },
    { "name": "stale_server_id_not_resent", "...": "..." },
    { "name": "clear_messages_clears_local_store", "...": "..." }
  ]
}
```

Invariants every scenario pins (these are the parity guarantees):
1. `ephemeral: true` present in each outbound `runs/` body.
2. **Full history re-sent every turn** — turn *N* body carries `2N-1` messages.
3. **No real server `conversationId` is ever sent or reused** (closes the
   iOS↔Android divergence; Android tests this, iOS must too).
4. After the run, the local store contains exactly the expected conversation; the
   server is **never** queried for history (`forbiddenCalls` not observed).
5. `clearMessages`/delete empties the local store.

### B2. iOS parity test
**New file:** `clients/agent-ios/Tests/.../EphemeralContractParityTests.swift`
- Load `contract.json` (reuse the `SSEFixture` path-walking helper).
- Use `MockURLProtocol` to **capture every outbound request body**; replay the
  per-turn SSE fixture as the response.
- For each scenario/turn: assert captured body == `expectedRequests[i]` (normalize key
  order; compare `messages` array exactly).
- Assert `LocalHistoryStore` contents == `expectedLocalStore`.
- Assert none of `forbiddenCalls` were issued.
- Backfill the **stale-id** scenario iOS is currently missing.

### B3. Android parity test
**New file:** `clients/agent-android/src/test/.../EphemeralContractParityTest.kt`
- Load the **same** `contract.json` (the fixtures walker already climbs to
  `test-harness/fixtures/`).
- Use `MockWebServer`; read each `RecordedRequest` body and assert == `expectedRequests[i]`.
- Assert `LocalHistoryStore` contents and `forbiddenCalls` exactly as iOS.

### B4. Parity guard
- A canonicalization helper (sorted keys, normalized whitespace) shared in spirit
  across both tests so "equal" means the same thing on both platforms.
- **CI gate:** both `EphemeralContractParityTests` (iOS) and
  `EphemeralContractParityTest` (Android) must run against the **same committed
  `contract.json`**. Changing the spec forces both platforms to update together —
  that is the mechanism that keeps them consistent over time.
- Optional belt-and-braces: a tiny pytest that loads `contract.json` and validates its
  shape so a malformed spec fails fast in CI before the mobile jobs run.

---

## 5. Layer C — Client security tests (C3)

Several of these **fail today** and thereby define the hardening backlog. Mark them
`@Test`/`func test...` but track each failing one as a hardening ticket.

### C-TLS — transport
- iOS `test_http_backend_url_rejected` / Android `test_http_backend_url_rejected`:
  constructing the client with an `http://` backend URL must throw/refuse.
  *(Requires a small validation in `APIClient.swift:154` / `APIClientRequests.kt`
  request building — neither enforces https today.)*
- `test_release_build_has_no_cleartext_exceptions`:
  - iOS: assert no `NSAllowsArbitraryLoads` in the library/Example **release** Info.plist.
  - Android: assert `usesCleartextTraffic` is not `true` and a
    `network_security_config.xml` forbids cleartext in release.

### C-Store — at rest
- iOS `test_local_db_has_file_protection`: assert the SQLite file is created with
  `NSFileProtectionComplete` (currently opened via raw `sqlite3_open()` with no
  protection — `LocalHistoryStore.swift:81`).
- Android `test_local_data_excluded_from_backup`: assert `allowBackup=false` **or**
  backup rules exclude `agent_local_history.db` and the `agent_frontend` prefs;
  `test_local_db_encrypted` (target: SQLCipher / Jetpack Security).

### C-Token — credential handling
- iOS `test_token_stored_in_keychain_not_userdefaults` (today it's `UserDefaults`,
  `APIClient.swift:98`).
- iOS `test_token_not_in_sse_url`: SSE auth must be a header, not an
  `?anonymous_token=` query param (`ChatViewModel.swift:1140`) — query strings land in
  logs/proxies. At minimum assert the existing redaction helper covers every log path.
- Android `test_token_in_encrypted_prefs_not_plain` (today plain `SharedPreferences`,
  `APIClient.kt:79`).

### C-Leak — no content in logs
- Both: drive a full ephemeral run with a canary; capture platform logs
  (`OSLog`/`Logcat`); assert canary never appears (release config). iOS already gates
  content logs behind `#if DEBUG` — assert that holds.

---

## 6. Layer D — Modal egress tests (C4, our code only)

**Framework:** pytest with a mock httpx/OpenAI transport that records the outbound
request without hitting the network.
**New file:** `packages/python/django_agent_runtime/tests/test_modal_egress.py`

- `test_base_url_is_honored_and_https`: with `MODEL_PROVIDER="openai"` and
  `base_url="https://<modal>/v1"`, assert the client's outbound request goes to that
  host over https.
- `test_no_fallback_to_public_openai`: when a Modal `base_url` is configured, assert
  **no** request is ever made to `api.openai.com` (catch silent fallback).
- `test_tls_verification_not_disabled`: assert no `verify=False` / disabled cert
  checking anywhere in the LLM client construction path.
- `test_modal_key_never_logged`: capture logging across a run; assert the configured
  key/secret string never appears.
- `test_modal_key_never_persisted`: assert the key is not written into
  `AgentRun.metadata`, `input`, `output`, or any event payload.
- `test_modal_key_never_returned_to_client`: assert no API response / SSE event
  carries the credential.
- `test_anthropic_models_do_not_route_through_modal` (**documents a real footgun**):
  the Anthropic client has no `base_url` override (`agent_runtime_core/llm/anthropic.py`),
  so a `claude-*` model will hit Anthropic directly, **not** Modal. Assert/annotate
  this so no one assumes Claude models are self-hosted. If they must be, that's a
  separate code change (route via the openai-compatible client).

---

## 7. CI wiring & how to run

| Layer | Command | Where it runs |
|-------|---------|---------------|
| A, D (server) | `pytest packages/python/django_agent_runtime/tests/test_ephemeral_invariants.py packages/python/django_agent_runtime/tests/test_modal_egress.py` | Backend CI job |
| B-iOS, C-iOS | `scripts/` → `swift test` (extend existing scripts) | macOS CI runner |
| B-Android, C-Android | `./gradlew testDebugUnitTest` (+ Robolectric) | Linux CI runner |
| Spec shape guard | `pytest` over `contract.json` | Backend CI job (fast pre-gate) |

- The **parity gate** is the important CI rule: a PR that edits `contract.json` must
  pass *both* the iOS and Android parity jobs. Wire this as a required check.
- Keep the existing `test-stub-server` for example-app/UI smoke (`run_ios_demo_test.sh`,
  Android `ChatStreamingUITests`); the parity layer uses in-process mocks
  (`MockURLProtocol` / `MockWebServer`) for determinism and speed.
- Add an end-to-end smoke (extend `real_backend_smoke.py`) that runs one ephemeral
  turn against a real backend and then calls `cleanup_ephemeral` + the canary DB scan,
  proving C1 against the genuine stack, not just unit mocks.

---

## 8. Execution order (suggested)

1. **§2 hard-delete change** + `EPHEMERAL_HARD_DELETE` / window=0 support.
2. **Layer A** server invariants + canary (proves C1; cheapest, highest value).
3. **Layer B** shared `contract.json` + iOS & Android parity tests (proves C2; closes
   the known divergence).
4. **Layer D** Modal egress tests (proves C4).
5. **Layer C** security tests — land the tests first (they document the gaps as failing
   checks), then work the hardening backlog they define (Keychain/EncryptedSharedPrefs,
   file protection, https enforcement, token-not-in-URL, backup exclusion).

---

## 9. Open decisions

1. Per-agent vs global pickup window once hard-delete lands — recommend per-agent
   override, global default 0 for privacy-first agents.
2. Whether to keep a content-free analytics row on cleanup (run counts/tokens) or
   delete the run entirely. Recommend content-free row.
3. Layer C hardening sequencing — which device-loss/MITM vectors are in the actual
   threat model for v1 vs deferred.
4. If Claude models must be self-hosted on Modal, route them via the
   openai-compatible client (separate change; §6 currently only asserts the footgun).
</content>
</invoke>
