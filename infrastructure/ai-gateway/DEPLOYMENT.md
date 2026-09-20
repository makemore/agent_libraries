# Deployment status — 2026-09-19

## Current: guarded public model catalog — deployed and boundary-verified

Public `GET /v1/models` is implemented and deployed through Caddy to the narrow guard at
`127.0.0.1:9092`, which queries Bifrost only at `127.0.0.1:8080`. Older records below that say
models are private or return 404 describe the **pre-catalog deployment**, not the current route.

### Public contract

- Exactly one canonical `Authorization: Bearer sk-bf-*` header; no cookie/alternate-header
  authentication or query strings. Query-bearing catalog requests return 400.
- Missing/malformed credentials return 401. Syntactically valid invalid/revoked keys and
  **all empty catalogs**, including valid keys with no grants/models, return 403.
- Nonempty success projects only validated, deduplicated IDs with `object: model` and derived
  `owned_by` fields in the list envelope. No provider-key IDs, key status or upstream diagnostics.
  Guard/upstream failure or invalid upstream responses fail closed with 502.
- Bifrost must authenticate and scope nonempty `data` to saved virtual-key grants. This is a
  **pinned-image contract**, not proof supplied by Bearer syntax or the offline guard tests.
  The real pinned-image multi-provider isolation proof passed and remains an image-upgrade gate.
- Clients may list explicit models or the provider catalog allowed by their **saved key**,
  never the global catalog. The guard needs **zero extra provider/admin credentials** and
  does not read gateway configuration, retained data or bootstrap secrets.

### Rollout and real systemd correction

- Infrastructure changes were **gateway VM startup metadata only**. Bifrost/Caddy image pins,
  disks, existing provider keys and persisted configurations were untouched; no additional
  provider/admin credentials were provisioned. No IAM, network or Studio changes.
- The first real systemd attempt stalled during `DynamicUser` allocation, before Python exec.
  An NSS/pre-exec stall was observed on this OS Login-enabled VM; OS Login is environmental
  context, **not a proven causal diagnosis**. A dedicated local non-login system account,
  `ai-gateway-catalog`, resolved the observed stall.
- Masking all of `/run` also caused `226/NAMESPACE`. The corrected unit masks **exactly**
  `/opt/ai-gateway`, `/srv/ai-gateway` and `/run/ai-gateway`, leaving systemd runtime paths usable.
- Networking permits `AF_UNIX AF_INET`, with `IPAddressDeny=any`,
  `IPAddressAllow=127.0.0.1/32`, `SocketBindDeny=any` and `SocketBindAllow=ipv4:tcp:9092`.
  The listener remains loopback-only. All other protective settings remain: strict filesystem
  protection, private temporary files/devices, no new privileges or capabilities, kernel/control-group
  protections, resource limits, no core dumps and no stdout/stderr logs.
- The corrected sandbox was verified on the VM for Python imports and socket binding.
  The **second rollout's startup succeeded**; `ai-gateway.service`,
  `ai-gateway-model-catalog.service` and `ai-gateway-iap-auth.service` were all active with
  **zero restarts** at verification.
- Gateway ordering uses `Wants`/`After`, **not `Requires`**, for the catalog unit. A catalog
  service failure produces catalog 502s without stopping inference.

### Recorded verification and remaining checks

- **31 offline guard tests passed**; these use synthetic upstream responses, not real authentication.
- The **84-test gateway suite passed with one opt-in admin test skipped**. The actual
  pinned-container admin test **passed separately**; its skip is recorded independently.
- **26 mocked OpenTofu tests passed**.
- **Real pinned-image multi-provider catalog isolation passed** using the unchanged guard in
  an already-local Python 3.11+ image, with already-local pinned Bifrost/Caddy images. Actual
  Python HTTP requests exercised stored grants, exact model identities, cross-key isolation,
  invalid/revoked/no-grant/empty cases and metadata projection. The disposable synthetic provider
  catalogs made no external provider calls; this proof required **no new image/package downloads**.
  It is not evidence of paid inference or production TLS. Reproduction commands and the separate
  older image-pulling smoke tests are in [local verification](README.md#local-verification-no-deployment).
- Latest-rollout TLS/hostname checks passed: health 200, catalog missing key 401,
  invalid key 403, query 400, management `/api/config` 404. Catalog responses use
  `Cache-Control: no-store`. Public ports 8080/8081/9091/9092 remain unreachable.
- Post-deployment OpenTofu plan exited 0 (no drift). Corrected policy/runtime tests
  and all 26 mocked plans were rerun successfully. Positive discovery/inference
  using a production virtual key remains a user-configured acceptance check.

## Historical record: initial deployment

- Project: `makemoredigital2025` (number `1048662406991`).
- Instance: `ai-gateway`, `europe-west2-b`, `e2-medium`.
- Reserved IPv4: **34.39.28.3**.
- State: `gs://makemoredigital2025-tfstate/ai-gateway/state` (existing private, versioned bucket).
- Applied 15 new gateway resources; no existing Studio resource updates or deletions.
- Enabled the IAP API as a project prerequisite; other existing API lifecycles were unchanged.
- Operator: `chris.barry@makemoredigital.com`, with instance/identity-scoped grants.
- Bootstrap Secret Manager version: `ai-gateway-bootstrap-json/versions/1`.
  Generated in memory and uploaded over stdin, not stored in Git, variables, or local credential files.
- No real provider credentials or client virtual keys were provisioned.

## Historical record: Google-protected browser admin — HTTPS active, dashboard fix deployed

The IAP browser-admin addition is deployed on 2026-09-19:
- URL: **https://llms-admin.makemoredigital.com**.
- New reserved global IPv4: **34.110.134.124** (not the inference VM address).
- Allowed browser identity: `chris.barry@makemoredigital.com`.
- Google-managed OAuth; backend-scoped IAP IAM; Bifrost's own login retained.
- Applied 12 new admin resources and a gateway VM metadata update, with no
  replacements/deletions or Studio resource changes. Post-apply plan has no drift.

The operator added this GoDaddy record, leaving `llms` unchanged. Both authoritative
nameservers and Google/Cloudflare public resolvers now return the expected address:

| Type | Name | Value |
| --- | --- | --- |
| A | `llms-admin` | `34.110.134.124` |

The managed certificate is **ACTIVE**; normal TLS/hostname verification and the Google
sign-in redirect pass for both the dashboard and its configuration API. The backend is
HEALTHY. No conflicting AAAA or parent CAA records were found at verification.
The operator reached Bifrost through Google but reported a dashboard configuration error;
the proxy correction below is deployed, awaiting the operator's browser retry. Final
allowed-account dashboard and ungranted-account checks remain interactive verification.
Architecture, security boundaries and operations are in [ADMIN.md](ADMIN.md).

### Dashboard query-string correction

- Root cause: Caddy's `forward_auth` target `/verify` inherited the dashboard's
  `?from_db=false`. The verifier intentionally accepts only the exact `/verify` target,
  so query-bearing requests returned 404 before reaching Bifrost.
- Fixed with `uri /verify?`: clear the cloned authentication subrequest's query only.
  Retain the original Bifrost request query, signed-token checks and Bifrost login.
- A pinned-container regression reproduced the 404 before the fix, then passed with
  real cookie login/config/logout and query-bearing authentication-denial checks.
  Both pinned-container tests, 60 Python unit tests and 26 mocked plans pass (exit 0).
- Applied only the gateway VM's rendered `admin.caddy` startup metadata and reran startup
  successfully. No IAM/network/storage/Studio changes; no stored Bifrost settings or
  credentials changed. Post-deployment plan reports no drift (exit 0).
- Live query-bearing requests now reach authentication (401 for missing/invalid IAP
  assertions instead of the erroneous 404). Services and backend are healthy; private
  login/config/logout, public HTTPS/route boundaries and blocked admin ports all pass.
  A valid Google browser session is still needed for final end-to-end confirmation.

Completed before DNS cutover:
- Runtime startup succeeded; gateway and sandboxed IAP verifier are active with zero
  restarts at verification. Google reports the admin backend HEALTHY.
- VM-side admin routes return 401 without a signed assertion, reject unsigned identity
  headers and a forged ES256 signature using a real Google key ID; wrong Host returns 404.
- Internet probes cannot connect to VM ports 8080, 8081 or 9091.
- Public API HTTPS health remains 200, admin/models remain 404 and missing-key inference 401.
- The private fallback still completes an actual Bifrost login/config/logout successfully.
- 26 isolated OpenTofu plans, 60 Python unit tests and two pinned-container smoke tests pass.
  All 34 verifier tests also pass on the VM's existing system Python/PyJWT version.

## Private admin login / fallback

Run a private local tunnel (keep this terminal open):

<augment_code_snippet mode="EXCERPT">
````sh
gcloud compute ssh ai-gateway --project=makemoredigital2025 \
  --zone=europe-west2-b --tunnel-through-iap -- \
  -N -L 127.0.0.1:18080:127.0.0.1:8080 \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
````
</augment_code_snippet>

Open **http://127.0.0.1:18080**. Username: `admin`.
In the [Secret Manager console](https://console.cloud.google.com/security/secret-manager/secret/ai-gateway-bootstrap-json/versions?project=makemoredigital2025),
view version 1 and use its `BIFROST_ADMIN_PASSWORD` value. Never paste this JSON into chat,
logs, Terraform, or shell arguments. Preserve `BIFROST_ENCRYPTION_KEY` with the database/backups.
The admin interface is intentionally not exposed on the public **inference** domain.
The separate browser-admin hostname above requires Google IAP plus Bifrost authentication.

The deployment session opened this tunnel locally; if it has ended or the computer sleeps,
run the command again. If port 18080 is already occupied by a working tunnel, reuse it.

## Historical record: public DNS / HTTPS verified before catalog rollout

The domain's authoritative DNS is GoDaddy (`ns11.domaincontrol.com`, `ns12.domaincontrol.com`).
The operator added the following record after deployment; both authoritative nameservers and
Google/Cloudflare public resolvers returned the expected address:

| Type | Name | Value | TTL |
| --- | --- | --- | --- |
| A | `llms` | `34.39.28.3` | 600 seconds or provider default |

No nameservers or unrelated records were changed; no Cloud DNS zone was created.
Initial ACME attempts failed with NXDOMAIN before the record existed. After DNS propagation,
only the Caddy container was restarted to retry issuance; certificate validation now succeeds.

Verified public HTTPS on 2026-09-19, **before the guarded catalog rollout**:
- `/healthz` returns 200.
- `/`, `/api/config`, `/api/providers`, and `/v1/models` return 404.
- All four allowed inference routes reject requests without a virtual key with 401.

These checks connected to the reserved IP with the real hostname for TLS SNI and normal
certificate/hostname validation. The local workstation resolver was still negative-cached at
verification; ordinary hostname access there may lag the public resolvers. Private admin access
does not depend on public DNS. Authenticated provider routing remains a separate pre-traffic check.

## Historical record: initial verification completed

- Startup service completed successfully; gateway running with zero service restarts at verification.
- Retained data disk mounted as ext4; Bifrost listens only on `127.0.0.1:8080`.
- Caddy listens on ports 80/443; IAP SSH and local forwarding work.
- Through IAP: health and UI return 200; unauthenticated `/api/config` returns 401.
- Actual `/api/session/login` succeeds; its session authenticates management APIs.
  The verification session was logged out; no password/cookie/response content was printed.
- Initial online SQLite backup succeeded; daily timer is scheduled at 03:00 UTC.
- Data disk's daily 04:00 UTC snapshot policy is attached. A scheduled snapshot/restore drill has
  not yet been exercised; perform an isolated restore test before relying on recovery.
- Local configuration tests and pinned-container auth/route tests passed before deployment.

Before client traffic, add provider keys through the private UI, scope virtual keys to explicit
client/provider-key/model combinations, and run positive routing and cross-client denial tests.

## Future infrastructure operations

The CLI login was refreshed, but the separate ADC login was expired. This deployment used a
short-lived CLI access token only in OpenTofu's child-process environment, not in arguments or
backend files. For normal future OpenTofu use, refresh ADC with
`gcloud auth application-default login`, then follow the main runbook. Always review the plan.
Deployment-specific nonsecret choices live in ignored `backend.hcl` and `terraform.tfvars`;
the operator grant is explicit and does not alter reusable defaults.