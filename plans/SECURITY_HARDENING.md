# Client security hardening checklist (consuming apps)

**Audience: the team shipping the iOS / Android app that embeds these libraries.**
The libraries default to secure behaviour, but a few controls live in *your* app
target and must be verified before a hardened (e.g. UK MoD) release. Items marked
✅ are enforced by the library; items marked ⬜ are **your** responsibility.

> See also `plans/ephemeral-security-validation-plan.md` for the full
> threat model and the server-side controls.

## Transport (network MITM / cleartext)

- ✅ The client **refuses cleartext HTTP** to a real backend (`APIClient.validateTransport`
  fail-closed; loopback / `10.0.2.2` / `.local` dev hosts and an explicit
  `allowInsecureHTTP` flag are the only exceptions).
- ⬜ Ship an **`https://` backend URL** in release config. Do **not** set
  `allowInsecureHTTP = true` in production.
- ⬜ **iOS App Transport Security**: the release `Info.plist` must NOT contain
  `NSAllowsArbitraryLoads` (the Example app sets it for local dev — do not copy it).
  Prefer the default ATS (TLS 1.2+, forward secrecy).
- ⬜ **Android cleartext**: release manifest must NOT set `usesCleartextTraffic="true"`.
  Ship a `network_security_config.xml` with `cleartextTrafficPermitted="false"` and
  reference it from `<application android:networkSecurityConfig=...>`.
- ⬜ **Certificate pinning** (recommended for field/hostile networks): pin the
  backend + Modal endpoint SPKI hashes — iOS via `URLSessionDelegate`
  challenge handling, Android via OkHttp `CertificatePinner`. (Library hook
  is a fast-follow; until then, configure pinning in the host app's session.)

## On-device data (device capture)

- ✅ Auth tokens + client memories are encrypted at rest (iOS Keychain;
  Android AES-256-GCM via the Keystore — `SecureStorageService`).
- ✅ iOS local conversation DB uses `NSFileProtectionCompleteUnlessOpen` and is
  excluded from backup.
- ⬜ **Android backup**: set `android:allowBackup="false"` in your `<application>`,
  **or** ship `dataExtractionRules` / `fullBackupContent` that exclude
  `databases/agent_local_history.db` and the `agent_frontend_secure` /
  `agent_frontend` preferences. (The libraries declare no `<application>`, so the
  default is whatever your manifest sets.)
- ⬜ **Android DB-at-rest beyond OS FBE**: the conversation DB is protected by
  OS file-based encryption today. For defence against root/forensic extraction,
  enable SQLCipher (tracked follow-up — adds native libs; test per ABI).
- ⬜ Call `viewModel.clearAllLocalData()` on **logout / sign-out** to wipe history,
  memories, transcript, and tokens.

## Egress / data sovereignty

- ✅ Per-run `private_only` routing is enforced **fail-closed** on the server
  (only the configured private/Modal endpoint; public + Anthropic providers blocked).
- ⬜ For data-sovereignty-restricted users, set `ChatWidgetConfig.privateOnly = true`.
- ⬜ Server: configure `PRIVATE_BASE_URL` (+ `PRIVATE_PROVIDER` / `PRIVATE_MODELS`).
  If unset, `private_only` runs fail closed (by design) — restricted users will
  error until it is configured.

## Build / logging hygiene

- ⬜ Release builds: no debug logging of message content (library content logs are
  `#if DEBUG` / absent; verify your own code too).
- ⬜ Android R8/ProGuard: keep the crypto + model classes; do not strip the
  Keystore code paths.
- ⬜ Confirm no secrets (tokens, keys) are committed to the app repo or CI logs.

## Operational (server) — verify before go-live

- ⬜ Schedule `cleanup_ephemeral` (cron / Celery beat) **and** alert if it has not
  run — the "no server history" guarantee depends on it.
- ⬜ Ensure DB backups/replicas taken during the ephemeral pickup window are also
  short-lived (or that the window is 0 for the strictest agents).
- ⬜ Independent penetration test / security review against your accreditation
  requirements before sign-off.
