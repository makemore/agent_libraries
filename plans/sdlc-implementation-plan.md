# SDLC implementation slices and acceptance plan

Status: **M0 implemented and isolated-tested; M1–M4 remain proposed.** 2026-09-17.
Source contract: [SDLC domain and approval contract](sdlc-domain-contract.md).
Local-first addition: [internal capabilities and provider sync](sdlc-local-first.md).
The completed ACE bridge validation is foundation evidence, not an SDLC test result.
Approval of this draft does not configure a host, provision identities or contact providers.

## 1. Package shape and dependencies

Owning local independent checkout: [agent/django_agent_sdlc](../agent/django_agent_sdlc/README.md).
It is not yet published, registered with checkout automation, or enabled in a host.
The table below retains the planned optional layout; M0 uses `models.py`,
`services.py`, `backends.py`, `views.py`, `forms.py` and `sdlc_tests/`.

| Area | Responsibility |
| --- | --- |
| `apps.py`, `models/`, `migrations/` | Core scope and internal work-tracking data/constraints; no startup workers or provider registration side effects. |
| `services/` | Only supported domain writes, authorization and revision/idempotency checks. |
| `policy/` | Core scope backend/access rules; human-gate evaluation belongs to the optional delivery app. No executable policy DSL. |
| `delivery/` | Optional Django app with delivery models/migrations, `workflows/` and `activities/`, using runtime and the shared ACE bridge. No imports from core AppConfig. |
| `api/`, `urls.py` | Authorized API and explicit mount; no dependency on Studio. |
| `views/`, `templates/django_agent_sdlc/` | Small usable internal tracker UI; no Studio requirement or new SPA/toolchain. |
| `integrations/local_git/`, `sync/` | Exact local repository boundary; optional sync app owns bindings/transfers/mappings and inbox/outbox, using ACE without runtime/delivery dependencies. Added with their milestones. |
| `integrations/github/`, `integrations/linear/`, `integrations/greptile/` | Provider-specific authenticated ingestion, normalized evidence and permitted outbound actions. Add only when implemented. |
| `studio/` | Optional Studio shell/navigation integration for the same authorized services/screens. |
| `tests/`, `docs/` | Isolated tests, host contract documentation and approved design. |

Core local tracking requires Django, not runtime, ACE, Studio or a provider SDK. The
delivery extra/app depends on runtime with its ACE bridge and compatible patched ACE
packages; the sync extra/app uses ACE without runtime, and Studio is an optional UI extra.
The host installs extras and enables prerequisite apps explicitly; AppConfig never
installs packages. Core migrations have no FK to an optional app. Resolve exact supported minima with installed-wheel tests,
not broad version metadata alone. Use package managers for manifests and
dependency changes, with approval for newly added dependencies. Do not change the live
host's app list, shared dependency versions or workspace membership during scaffolding.

## 2. Delivery milestones

### M0 — usable internal work tracking, without providers

**Implemented:** [package installation and usage](../agent/django_agent_sdlc/README.md)
and [implemented contracts/evidence](../agent/django_agent_sdlc/docs/contracts.md#verification-and-acceptance).
226 tests passed on private PostgreSQL 17 / Django 5.2.17; 216 passed with 10
PostgreSQL-only skips on SQLite and fresh wheel installs with Django 5.2.17/6.1.1.
Checks/migrations, wheel resources and lint passed. Counts overlap; no live host,
browser, provider, approval or execution test is implied. Status lanes are paginated;
checklist editing uses a simple JSON textarea. M1 is the next implementation slice.

Build the core package and built-in scope backend: create a private scope, explicitly
grant collaborators access, create/edit/assign/filter work items, add comments and
checklists, maintain revisioned notes, and view an authorized activity/attention list.
Provide a small status board/detail UI and authenticated APIs using the same services.
Tracker status never writes an ACE phase. No external account or agent run is needed.

- Honor omitted internal defaults and previously stored backend choices. Reuse host
  scopes through their adapter when selected; never duplicate their membership roster.
- Use stable UUIDs/display numbers and revision/idempotency checks from the start.
  Keep comments/notes local; do not create a dormant outbound export on every edit.
- Separate the tracker and optional delivery app/migrations so installation without
  ACE/runtime/Studio actually works. No live demo population or new shared dependencies.
- Reserve the typed capability/link boundary; advertise only working backends. Do not
  display functional GitHub/Linear sync controls before those adapters exist.
- Gate: L01–L05 and core-only portions of L06–L07, fresh-install/migration checks and
  authorized stored-state/API readback. L07 publishing/approval assertions complete in
  M1; L06 artifact assertions in M2 and sync-payload assertions in M3. M0 does not claim
  to implement approval/execution or provider sync.

### M1 — specification approval to one real runtime execution

Smallest end-to-end slice: open a scoped delivery → persist a specification revision →
open review → eligible human approves → authorize gate use → dispatch one engineering
agent through ACE → persist its actual runtime outcome and supported history readback.

- Reuse M0 scopes/work items and implement policy/role bindings, delivery/spec/attempt/
  request and review/decision/gate-use records in the optional delivery app, service
  invariants, one ACE workflow, resolver/authorization hooks and authenticated APIs.
  Publish user-authored note/spec revisions through services; analyst automation is
  not necessary to establish the write/approval boundary.
- Use a provider-free registered agent and a DB-local test host adapter with real user
  grants. Runtime submit/runner/history and ACE inbox/worker/reconciliation are real;
  cut only external transport/provider I/O at their boundaries.
- Fix execution principal at attempt creation. Explicitly reject different role users
  in this slice; do not fake identity isolation with metadata.
- Complete with a neutral `engineering_result_recorded` result. This deliberately
  partial workflow is not the full lifecycle and must not claim a release authorization.
- Gate: A01–A15 and L07, migration/installation checks, and clean PostgreSQL teardown.

### M2 — complete provider-free delivery loop

Add candidate/evidence records, typed evidence validation, QA work, human release gate,
controlled rework and current-versus-historical UI/API projections. Add exact local Git
bindings and internal change/check evidence so a hosted PR or CI provider is not a
prerequisite. Real local checks still need a supported, authorized executor; manual
attestation is never labelled automated CI. Preserve host-required evidence policies.
Test provider adapters deliver named synthetic facts through the production evidence service; they
do not create fake completed AgentRuns or bypass the runtime history writer.

Gate: A16–A21, L06 artifact checks and L08–L09 plus repeat M1.
Finish at `release_authorized`, never auto-merge/release.
If per-role users are selected, implement separately bound role workflows and durable
correlation/cancellation here and test them before any real multi-role pilot. This is
additional domain choreography, not an implicit capability of the current bridge.

### M3 — real provider boundaries, still no automatic merge

Implement only selected providers/actions behind capability-specific boundaries.
Code hosting and issue tracking are separate choices: GitHub can supply code/check
identity while issues stay internal; Linear or GitHub Issues can later own mapped
tracker fields. Greptile supplies review evidence only if chosen. None is required
for core SDLC. Use the verified Warp/Shipwright/Conduit source inventory in the
[local-first contract](sdlc-local-first.md#5-existing-warp-integrations-reuse-with-boundaries),
not a new copy of host account models or credential-bearing clone URLs.

Implement internal/linked/mirror/external authority modes, stable links, field/status/
identity mappings, read-only transfer preview and explicit resumable apply, conflicts,
pause/disconnect/reconnect and provider switching. First exercise the transport boundary
with a deterministic test adapter through real services/inbox/outbox; then add selected
real connectors. Unsupported conditional-write/two-way capabilities stay disabled.

Inbound contract:

1. Verify signatures using the provider's documented raw-body scheme, validate the
   installation/workspace-to-scope binding, enforce size/schema limits and reject
   unknown event types. No user identity or approval authority from webhook fields.
2. Store a minimal `ProviderDelivery` receipt and allowlisted normalized fact envelope
   atomically, deduplicated by provider + instance + installation + delivery ID. Keep a private
   integrity digest; a conflicting duplicate is an error, not replacement evidence.
3. Route durable pending receipts by fact type through supported services. Issue,
   comment and note facts enter tracker services, even with no delivery/runtime app.
   Only delivery-evidence facts enter `record_evidence`; that evidence and its durable
   workflow notification commit together. A crash after acknowledgement cannot lose
   the only copy of work. No raw payload/header/credential logging.
4. Old head/job-attempt observations never overwrite newer ones. Provider timestamps
   alone do not order events reliably. Re-fetch authoritative current state when
   needed outside DB locks, then validate subject/attempt before accepting facts.

Outbound contract:

- `ExternalOperation` records scope, stable business operation key, typed subject
  reference, grant reference, action and acknowledgement/uncertainty state. Tracker
  operations have no mandatory attempt FK; optional delivery integration adds its
  own association/authorization. No arbitrary model import from a reference string.
- Use existing vault/managed MCP grants where appropriate; other provider adapters
  use the host's secret service. No plaintext tokens in new SDLC models or arguments.
- Reauthorize immediately before external I/O and use provider-supported conditional
  writes against the expected revision when available. Do not hold DB locks across
  network calls or claim atomic transactions between PostgreSQL and a provider.
- Use provider idempotency where supported. After a lost acknowledgement without that
  support, discover/reconcile using durable correlation; an ambiguous effect remains
  `uncertain` for operator handling instead of blindly creating another PR/comment.
- Prevent feedback loops: tag/correlate SDLC-originated updates and ingest them as
  observations, not new delivery requests. A Linear status edit cannot approve a gate.
- Adapters/tool grants must exclude merge/deploy actions in v1, not merely omit their
  buttons. General shell or MCP tools must not bypass the human-release boundary.

Gate: A22–A26, L06 sync-payload checks and L10–L18 with provider-shaped fixtures
before approved live smoke tests.
No API capability (signing, dedupe, conditional writes) is assumed across providers;
verify current provider documentation during adapter implementation.

### M4 — optional Studio UI and one opted-in pilot

Integrate existing tracker/delivery screens with Studio; include exact review subject/
evidence, decision/rework forms, provider authority/sync status and operator recovery.
Use Studio shell blocks; add one small navigation contract if required, with
server-side checks on every route/API regardless of links.
Do not repurpose read-only thread sharing as execution-principal impersonation.

Gate: A27–A30, installed-package compatibility, identity/ACL/runtime target revocation
checks and human-approved pilot configuration from the domain contract. No demo writes
until a reusable owning-repository seed has preview, explicit targets, idempotency and
isolated tests. Test reruns must preserve user-owned configuration/history.

## 3. Acceptance test catalogue

These IDs identify tests **to write and run**, not claims of coverage already present.
Use unit tests for policy/transition rules, service/API tests for persisted state, and
separate OS processes on PostgreSQL for lock/crash claims. Assert both data and authorized
read APIs; HTTP 200, model mocks or rendered screens alone are insufficient.

| ID | Scenario | Required observable result |
| --- | --- | --- |
| A01 | Core without runtime/ACE/Studio; optional sync with ACE only; optional delivery app; ordinary runtime without SDLC | Each supported combination passes imports/checks; core CRUD has no execution dependencies, optional prerequisites are explicit, runtime behavior unchanged. |
| A02 | Concurrent duplicate delivery/attempt start | One active attempt, ACE workflow and binding; no unbound dispatch; rollback leaves no partial setup. |
| A03 | Unknown/foreign scope, forged actor metadata, missing configured adapter | Denied before writes; no data/count leakage; no fallback to internal/admin authority on host-adapter failure. |
| A04 | Default history path | Blank agent storage override; supported runtime execution writes canonical history; authorized read returns it. |
| A05 | Named JSON compatibility/privacy exception | Existing host choice respected; ephemeral/redacted evidence cannot authorize a gate or get copied into durable history. |
| A06 | Independent human approves a current spec | Decision and signal commit atomically; one gate use and one engineering run; principal owns the run. |
| A07 | Bot, author, execution principal, viewer or ungranted admin approves | No qualifying vote, even if logged in or sending forged human metadata. |
| A08 | Replayed/conflicting decision command | Identical authorized request reuses record; changed subject/action conflicts; denied retry reveals no protected result. |
| A09 | Concurrent votes, withdrawal and quorum consumption | No double-counted human; one action receipt; both serialized withdrawal/action orders preserve the contract. |
| A10 | Spec edit races approval/dispatch | Old attempt immediately fenced; no stale authorized run, even before invalidation notice delivery. |
| A11 | Approver/principal/grant revocation while waiting or queued | Consumption/execution denies; no fake success; active-work cancellation is reported as cooperative. |
| A12 | Crash after decision or gate-use write, before commit/after commit before ACK | Before: all related writes roll back. After: same identifiers on retry, no duplicate signal/action. |
| A13 | Outcome before activity ACK; duplicate and reordered signals | No lost progress or duplicate request; old generation never resets current phase. |
| A14 | Blocked workflow, expiry, policy retirement/revocation and recovery | Votes retain history; expired/revoked authority never advances protected work after resume. |
| A15 | Role-user mismatch, entry-agent/configuration drift, target denial | Fail closed; no active-version or credential fallback; actual loader/dispatch identity matches approved request. |
| A16 | New head/base/target on same PR | Old review unusable; old-head evidence remains historical, not current. |
| A17 | Missing/unknown/skipped/failed checks or forged evidence | Gate stays closed; run `succeeded` alone never counts as QA/check success. |
| A18 | Purged/redacted/unreadable or retracted required evidence | Gate fails unavailable; authorized list/detail does not recover transcript copies. |
| A19 | QA failure and bounded business rework | Explicit new request/candidate/generation; technical retries keep their old key; budget exhaustion waits for a human. |
| A20 | Final human gate | Ends at release authorization for the precise subject; zero merge/deploy calls. |
| A21 | Distinct-principal role workflow option, if implemented | Separate run owners; atomic correlation, duplicate forwarding and cancellation recovery; no cross-owner history leak. |
| A22 | Valid/invalid provider signatures and wrong installation | Only authenticated, correctly scoped events receive accepted receipts; rejects do not alter domain state. |
| A23 | Duplicate/conflicting/out-of-order webhooks, including separate provider instances | One durable receipt per instance-qualified identity; identical installation/delivery IDs from different instances do not collide; conflicts surfaced and newer facts not overwritten. |
| A24 | Crash after inbound ACK before workflow notification | Pending receipt recovers through services; evidence/notification commit together once. |
| A25 | External success followed by lost ACK | Stable correlation recovers existing effect or remains uncertain; no blind duplicate mutation. |
| A26 | Credential revocation, changed remote HEAD, reflected status event | Deny/conditional conflict/uncertain result as appropriate; no credential fallback or event loop. |
| A27 | Studio viewer/direct API access and CSRF | Hidden navigation is not the security boundary; unauthorized reads/writes and unprotected session POSTs rejected. |
| A28 | Reviewer needs evidence but does not own run | Only allowed projection/share visible; no raw runtime response under service-user impersonation. |
| A29 | Defaults, preview and repeated seed | Preview writes nothing; supported writers used; rerun preserves configuration, ACLs and history. |
| A30 | Release/install/host smoke | Patched wheel combination and explicit URLs work; role scopes/worker/reconciler/retention verified, no host-default reset. |

### Local-first and synchronization additions

| ID | Scenario | Required observable result |
| --- | --- | --- |
| L01 | Fresh internal setup, no providers or execution apps | Authenticated user creates a private scope and issue via UI/API; stored state/readback agrees; no network calls, agent runs or sync jobs. |
| L02 | Collaborator roles, assignment and revocation | Only explicit grants permit access; assigning a user grants nothing; revoked users lose list/count/detail/write access; managers are not automatically approvers. |
| L03 | Internal defaults vs explicit host backend | Omission uses supported internal defaults for new scopes; reruns and changed defaults preserve existing choices; invalid host adapters never fall back to local grants. |
| L04 | Concurrent issue creation/edit and repeated commands | Stable unique display numbers/UUIDs, one write per key, stale revisions conflict; no lost updates or duplicated comments. |
| L05 | Internal comments/checklists/notes/activity UI | Authorized stored data and API/UI agree; edits retain permitted provenance, Markdown is safe, GET/preview writes nothing and session mutations require CSRF. |
| L06 | Local privacy/retention and artifacts | No transcript copies/public artifact URLs/automatic exports; deleted or expired content is not recoverable through revisions, activity or sync payloads. |
| L07 | Work item, note and delivery separation | Ordinary work needs no delivery policy; publishing creates a fixed spec revision; later edits/status changes neither approve nor restart/complete a delivery. |
| L08 | Local Git without hosted provider | Exact repository/commit subject, internal change review and supported check execution work in a disposable repo; no fake PR/CI result; manual evidence cannot satisfy required automated checks. |
| L09 | Local repository safety | Dirty user worktrees remain unchanged; path/option/config injection and unsafe protocols denied; no credential-bearing argv/logs, arbitrary Git commands or shared-checkout reset. |
| L10 | Enable one capability/provider; inbound issue/comment without a delivery | Code-host connection leaves tracker/storage/ACL/history defaults unchanged; tracker sync uses tracker services without creating a delivery/evidence; missing capabilities are explicit, never silently emulated. |
| L11 | Read-only transfer preview then stale apply | Preview performs no local/remote mutations; exact destination/audience/selection is shown; changed revisions or grants reject apply; expired credentials require separate renewal. |
| L12 | Internal → mirror → external → internal/provider switch | Stable IDs, authorized history and delivery/spec/decision links survive; cutover covers the complete capability population and fences new creation; a selected-items export cannot switch unselected authority; local authority returns only by explicit transfer. |
| L13 | Export/import audience and retention | Private comments/restricted evidence excluded unless specifically authorized; source ACLs constrain projection; source grant alone grants no local viewer access; redaction reaches baselines/pending payloads. |
| L14 | Same-field divergence, status/user mapping gaps | No last-writer-wins; explicit revision-bound conflict resolution; unknown status remains unresolved and external user never becomes a local approver/grant. |
| L15 | Duplicate/echo/out-of-order sync; lost ACK | One logical object/command, no loop or duplicate creation; stable correlation reconciles an effect or leaves uncertainty, never blind retry. |
| L16 | Pause/disconnect/reconnect and source deletion | Stale externally owned fields remain read-only/unavailable; no automatic local fallback; reconnect reuses links; remote deletion does not delete local approval history. |
| L17 | Cutover races/crashes, concurrent item creation and in-flight effects | Both serialized orders tested on PostgreSQL; stale-generation jobs denied, new items cannot escape the fence, checkpoints resume, uncertainty blocks activation and abort never silently deletes remote objects. |
| L18 | Adapter lacks versioned writes or loses permission | Unsafe editable two-way mode unavailable without explicit exception policy/tests; command remains pending/denied/uncertain rather than false success; no fallback credentials. |

## 4. Verification sequence and completion evidence

Run isolated Django/pytest with the meta-repo root `.venv/bin/python`, never host
settings. New package test settings must be unable to resolve a live DB from inherited
environment values. Extend the proven [disposable PostgreSQL harness pattern](../agent/django_agent_runtime/tests/run_ace_postgres.py)
for SDLC in its owning repository; do not aim bridge test settings at a host database.

Implemented M0 tests and proposed later targets (later files do not yet exist):

| Target | Focus |
| --- | --- |
| `sdlc_tests/test_services.py`, `sdlc_tests/test_api.py` | Implemented M0 CRUD, notes/checklists/activity, ACLs/backends, idempotency, safe UI/CSRF and persisted readback. |
| `sdlc_tests/test_postgres.py` | Implemented separate-process allocation, duplicate writes, stale edits, ACL serialization and crash/lost-ACK checks. |
| `sdlc_tests/test_install.py`, `sdlc_tests/test_harness.py` | Implemented core-only resources/imports, installed-wheel origin, static discovery and driver isolation; run_package/run_postgres drivers include checks/migrations. |
| `tests/test_gate_policy.py` | Exact subjects, human eligibility, quorum, expiry and invalidation. |
| `tests/test_delivery_services.py` | Supported writes, atomic decisions/signals, idempotency and scope consistency. |
| `tests/test_delivery_api.py` | Authorized read/write projections, foreign-scope denial and actor forgery. |
| `tests/test_delivery_workflow.py` | Real ACE/runtime path, default history readback and reordered receipts. |
| `tests/test_delivery_postgres.py` | Separate-process start/vote/revocation races and transaction-boundary crashes. |
| `tests/test_local_git.py` | Disposable repository tests for exact subjects, dirty-worktree preservation and command/credential safety. |
| `tests/test_provider_sync.py`, `tests/test_sync_postgres.py` | Transfer modes/previews, mappings, conflicts, privacy, echo prevention, reconnect and cutover/crash races. |

For each milestone: smallest failing policy/service test → test file → scoped suite →
PostgreSQL concurrency/crash selection → API/optional UI checks. Add migration-drift,
dependency-direction/import, installed-wheel and scoped lint checks. Document exact
commands, versions, exit codes, skipped cases and limitations. Do not sum overlapping
selections or label provider-free evidence as proof of real provider side effects.

Live enablement remains a separate, explicit decision after these tests and the host
choices in [the domain contract](sdlc-domain-contract.md#8-decisions-needed-before-a-live-pilot).
