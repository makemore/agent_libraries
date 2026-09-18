# Local-first SDLC capabilities and provider synchronization

Status: **M0 tracker implemented and isolated-tested; remaining capabilities and sync are design only.** 2026-09-17.
See [the package README](../packages/python/django_agent_sdlc/README.md) for implemented scope,
installation and verification. Nothing has been enabled in a live host.
The user requested an internal issue tracker and lightweight internal alternatives
that can later be superseded by or synchronized with external services. This updates
the [domain contract](sdlc-domain-contract.md) and [implementation plan](sdlc-implementation-plan.md).

## 1. Product decision

**Useful without SaaS; integrate by capability, not by replacing the whole app.**
New scopes start with internal work tracking. No GitHub/Linear account, OAuth grant,
MCP server, webhook, cloud bucket or broker is required to create and discuss work.
Ordinary Django authentication/database setup still applies. Agent execution needs
the runtime and its configured model/executor; durable delivery workflows need ACE.
"Provider-free" does not mean an unconfigured agent can execute real engineering work.

Keep the internal work tracker in `django_agent_sdlc`, not in runtime and not a new
general-purpose project-management product. Its local CRUD path must run without
Studio or ACE installed; workflow/sync integration is explicitly enabled separately.
This revises the original mandatory runtime/ACE package dependency proposal: use
optional extras/apps for those capabilities, with no startup workers or provider I/O.
The optional sync app uses ACE for durable transport but does not require agent runtime
or the delivery app. Its scope/object references must support a standalone work item.
Core models/migrations never reference optional delivery/sync tables; optional apps
own their links back to core. Installing an extra does not enable apps or workers.

External providers are opt-in per scope and capability. Connecting GitHub for code
does not change the issue tracker, storage, notifications or approval policy. Omitted
optional settings inherit supported defaults; new defaults never rewrite existing
bindings. Invalid or unavailable configured backends fail closed, not over to local.

## 2. Lightweight internal options

| Capability | Internal implementation | Optional external counterpart | Boundary |
| --- | --- | --- | --- |
| Work tracking | Issues/tasks/bugs, status list/board, priority, labels, assignee, comments and checklists. | GitHub Issues, Linear, Jira or another implemented tracker adapter. | Work-item status is not an ACE phase, approval or deployment result. |
| Notes and specifications | Small Markdown notes with immutable revisions; publish a selected revision as a delivery specification through its service. | Repository Markdown, issue descriptions, document/wiki adapters. | Remote edits create proposals/new revisions; never rewrite an approved spec. |
| Human reviews and decisions | Internal review forms and append-only revision-bound decisions. | External review evidence and authenticated notification/deep links. | No connector may replace human eligibility or turn issue closure into approval. |
| QA/check records | Internal acceptance checklists and typed manual/agent/check-run evidence. | CI providers and review tools such as Greptile. | Manual attestation is labelled as such; a policy requiring automated CI cannot be satisfied by a checkbox. |
| Code references | Explicit local Git repository binding, exact commits/diffs and internal change-review references; Git CLI is sufficient. | Hosted Git/PR adapters, including GitHub and GitLab. | No home-grown Git server or fake PRs; local commits and hosted PRs are different object types. |
| Artifacts | Authorized metadata/digests plus the host's configured Django storage backend. | Object storage or provider artifact references. | Inherit storage defaults; do not expose public media URLs, copy restricted artifacts or invent unlimited retention. |
| Activity and attention | In-app chronological activity and a filtered "needs my action" list derived from current permissions. | Email, Slack or other notifications. | No replacement chat server; delivery failure does not change domain authority. |
| Release record | Internal record of the exact subject authorized for release. | Hosted release/deployment observations. | Authorization is not merge, deploy or publication; humans retain those actions. |

Implement issues, notes/checklists and activity first. Most remaining internal options
are the already-planned specification/evidence/approval domain, not separate products.
Do not build a CI scheduler, wiki platform, secrets vault, artifact registry, generic
notification bus or arbitrary connector framework for this milestone.

### Minimal work-tracker model

- `WorkItem`: stable UUID, scope, immutable scope-local display number, kind
  (`task`, `bug`, `feature`), title/body, priority, optional assignee, bounded labels,
  current revision and tracker status (`backlog`, `ready`, `active`, `blocked`,
  `done`, `cancelled`). Internal creation defaults to backlog; no custom status DSL.
- `WorkItemRevision`: accepted field changes, actor/source attribution and revision.
  Compare expected revision on updates. Published audit entries are not editable.
- `WorkComment`: stable UUID, author/source, protected body and append-only edits or
  redaction markers. Internal comments stay internal unless deliberately exported.
- `WorkChecklistEntry`: stable item UUID, bounded text and completion state; reordering
  does not create a new identity. Completion is a task fact, never approval evidence
  without a separate typed evidence submission and policy validation.
- `WorkNote` / `WorkNoteRevision`: lightweight revisioned Markdown, optionally linked
  to a work item. Publishing a specification produces an immutable specification
  revision with provenance; a later note edit does not mutate the published revision.
- `DeliveryWorkItem`: explicit scope-checked link between planning work and delivery
  execution. A work item may have no delivery or several successive deliveries.
  Completing/reopening it neither completes nor silently restarts a workflow.

List/filter, small status board, detail/edit, assign, comment, checklist and archive
are sufficient. No sprints, estimates, epics, custom fields, time tracking or graph
scheduler in v1. Archive hides ordinary listings but is not a retention/delete policy.
Markdown rendering must escape/sanitize untrusted content; links are not authority.
Do not copy agent conversations into work comments or activity by default.

### Scope and identity without an external workspace

Provide a built-in `internal` scope backend using Django users, `DeliveryScope` and
small `LocalScopeGrant` rows. An explicitly created scope is private to its creator;
viewer/editor/manager grants are explicit. Ordinary active users may create their
own scope unless the host denies scope creation; there is no implicit superuser read.
This is a fixed access model, not a policy DSL or a second organization hierarchy.

For existing Studio/host projects, bind their immutable namespace/project key and
use the existing workspace backend rather than copying memberships into local grants.
The authorization backend/namespace is pinned when a scope is created. A host setting
may select the default backend for new scopes; changing it does not reinterpret old
scopes. Unknown backend, deleted project or host lookup error denies access. The
default backend is used only when configuration is omitted, never on adapter failure.

Grant changes and local writes serialize on the same scope/ACL parent. Delivery
commands retain the ACE-first lock order in the domain contract. A local owner or
manager does not automatically become a human approver or gain runtime/tool grants.
Issue assignment never grants project membership, credentials or execution rights.

## 3. Provider modes and stable identity

`CapabilityBindingRevision` records the scope/capability, connector reference,
authority mode, field mapping, allowed directions, content-export policy and generation.
Credential values stay in existing host secret services; bindings contain references.

| Mode | Authority and writes |
| --- | --- |
| `internal` (default) | Local authority; no external writes or outbound operations are created. |
| `linked` | Explicit external references/current observations only; no automatic field synchronization. |
| `mirror` | Local authority; selected fields are exported. Remote changes become visible conflicts/proposals, not overwrites. |
| `external` | One selected provider owns the mapped issue fields. SDLC presents an authorized projection and sends permitted edits as tracked commands; it shows pending/failed, not success before acknowledgement. |

Bidirectional transport is supported where the adapter permits it, but is not
last-writer-wins authority. Each synchronized field has one declared owner; optionally
split ownership with an explicit versioned map. Unmapped fields remain internal and
are labelled accordingly. Never allow two external writers for the same field.
Internal delivery decisions, gate uses and execution facts always remain SDLC-owned.

These field modes apply to tracker/document projections, not every capability in the
same way. Artifact-backend changes use an explicit authorized copy/verify/reference-
switch procedure with immutable artifact IDs/digests and source retention rules;
changing a storage default must not reinterpret existing artifact locations. Code-host
switches require repository/commit identity verification. Notification targets can be
changed independently but never gain authority to approve or execute work.

`ExternalLink` maps a stable internal object UUID to provider + provider-instance +
account/installation + remote object type/ID. Project/issue keys and URLs are display
data, not identity; cloud and self-hosted instances must not collide. A unique remote
mapping prevents accidental duplicate imports within a binding. Do not auto-link by
title, email or URL alone. Moving a linked issue across provider projects requires
fresh scope/ACL validation, not automatic reassignment of the internal scope.

`SyncCheckpoint` tracks the last mutually acknowledged local revision, remote version
and field digests; `SyncConflict` records competing revisions and resolution. Retain
only permitted content, with the original source and retention classification. Use
the planned `ProviderDelivery` inbox and `ExternalOperation` outbox for transport,
not another queue or a second writable delivery lifecycle.

## 4. Connect, switch, disconnect and reconnect

"At any time" means existing records do not require a new project or lost history.
It does not mean unreviewed bulk export, instant distributed atomicity or generic
support for providers whose adapters have not been implemented. Unresolved effects
or permission conflicts may block cutover; report them without guessing a winner.

1. **Preview:** select scope, capability, direction and exact provider destination.
   Discover existing mappings and calculate proposed creates/updates, retained local
   fields, status/identity mappings, audience changes, conflicts and unsupported data.
   Preview may perform explicitly authorized remote reads, never local/remote writes,
   webhook creation, credential refresh writes or outbox creation. Expired credentials
   require a separate connection action. Counts must not expose inaccessible items.
2. **Approve a transfer plan:** an authorized manager accepts the exact bounded
   selection, content classes, target audience, expected local/remote revisions and
   binding generation. Persist an immutable `TransferPlan` through an explicit write
   action, not a GET. Plan approval grants no human delivery approval or credential.
3. **Prepare cutover:** durably fence writes/old-generation operations for the selected
   capability. A scope/capability authority change covers every affected existing item
   and fences creation in that capability until activation or abort. A bounded batch
   is only a transport chunk, not permission to switch unreviewed/unmapped items. A
   selected-items export may add links but cannot change the scope-wide authority.
   Revalidate ACLs, source/target versions and outstanding operations.
   Drain, cancel or reconcile them; a lost-ACK effect must be resolved or the transfer
   remains blocked. Do not hold DB locks during network I/O. Read-only previews remain
   available while the UI exposes a bounded pending transfer, not a false completion.
4. **Copy/link through services:** import or export with stable object/operation keys
   and resumable checkpoints. Preserve local UUIDs, comments, specification links,
   gate history and source attribution. Unmapped remote users remain external actors,
   not silently created Django users or mapped approvers. Existing remote objects are
   linked explicitly, never inferred from similar titles. Backfills have batch limits.
5. **Activate:** atomically install the new binding revision only after the authorized
   capability's complete affected set is reconciled. Advance its generation; old cursors, jobs and receipts
   cannot authorize new writes. Before activation, abort leaves old authority in place;
   any already-created remote objects remain recorded for reconciliation, not silently
   deleted. Switching back after activation is another reviewed transfer.

### Normal sync and conflict handling

- Local edits and outbound intents commit together. Inbound receipts commit before
  acknowledgement and flow through the same domain services as internal writes.
  Reconcile after crashes; technical retries never manufacture new business identities.
  Route work-item/comment/note observations to tracker services without requiring a
  delivery. Only delivery-evidence facts enter evidence services/workflow notification.
- Compare local and remote values with the last acknowledged baseline. A remote-only
  change may update an externally owned field; a change against an outstanding local
  command is a conflict, not an optimistic success. Compatible disjoint-field changes
  may combine only under the declared map. Same-field divergence requires an explicit
  resolution bound to both revisions; resolution fails if either has changed again.
- Use provider-native version/conditional writes where available. A provider lacking
  conditional writes must not advertise race-free editable two-way sync. Restrict that
  capability to links/read-only import or explicitly approved weaker semantics with
  tests; re-fetch-then-write alone cannot guarantee prevention of a concurrent overwrite.
- Preserve operation correlation to suppress echoes. Record the originating provider
  delivery and outbound key. A reflected update is an observation, not a new task,
  comment, delivery attempt or vote. Do not rely on timestamps alone for ordering.
- Map status, labels, checklist and user fields explicitly. Unsupported/custom states
  stay unresolved and visible; never map unknown to `done`, failure to success or an
  external assignee to a new local grant. No automatic destructive delete propagation.
- A remote delete/archive becomes a source tombstone. Retain permitted local audit and
  surface source unavailability; do not cascade-delete comments or approval records.
  Privacy erasure/retention requests still follow their own supported cleanup path.

### Disconnect is not an authorization downgrade

Pause revokes new transport work and fences the connection generation. Keep link IDs,
permitted history and checkpoints; retain no credential in those records. Confirm
what happened to already-in-flight operations before claiming a clean disconnect.
An unavailable externally owned field is stale/read-only, never automatically local.
Users may continue permitted internal notes or other unaffected capabilities.

Returning to internal authority is an explicit transfer that materializes only content
the actor may retain. Inaccessible source content stays unavailable, not recovered
from caches. Reconnecting reuses stable links and reconciles changes since the last
checkpoint; it does not blindly replay an old outbox or create duplicates. Provider
switches follow the same path; remote deletions are never rollback compensation.

### Privacy and approval invariants

Local access is not permission to export. Require an explicit content allowlist and
audience/retention check for every destination; deny widening access by default.
Private comments, credentials, prompts, transcripts and restricted evidence are not
included in a general issue sync. Externally readable content is not automatically
readable by every local scope member: apply source ACL restrictions or an approved
retained-copy policy before import/projection. A service-account grant alone does not
prove the viewing human's source access. If it cannot be established, deny the read.

Content redaction/expiry also applies to sync baselines, previews and pending payloads;
store minimal tombstones/digests rather than an undeletable second archive. On grant
revocation, stale jobs cannot export previously readable content. Normal history is
preserved across provider switches only to the extent retention and privacy permit.

Binding changes invalidate affected pending commands immediately. A tracker-title or
label sync need not invalidate a published specification. Changing approved content,
evidence authority, repository/target identity or required evidence invokes the domain
generation/invalidation service; a synced issue status never authorizes execution.

## 5. Existing Warp integrations: reuse with boundaries

The relevant code is inside Warp's nested projects, not just `products/warp/backend`.
The following source was inspected; it has not been validated for SDLC production use.

| Existing source | Reusable shape | Work required before SDLC reuse |
| --- | --- | --- |
| [Shipwright provider interface](../products/shipwright/backend/integrations/base.py) | `GitProviderAdapter`, `AdapterCapabilities`, repository/PR DTOs. | Keep code-host capabilities separate from issue/sync/storage capabilities. No SDLC dependency on Shipwright's generic top-level `integrations` app. |
| [GitHub](../products/shipwright/backend/integrations/git/github.py) / [GitLab](../products/shipwright/backend/integrations/git/gitlab.py) | Repository discovery, file-at-ref, webhook and PR operations. | Add scoped grant resolution, bounded HTTP timeouts, safe errors and durable operation/reconciliation contracts. These interfaces are not an issue-sync implementation. |
| [Plain Git](../products/shipwright/backend/integrations/git/plain_git.py) | Represents a Git remote without a hosted API and declares unsupported operations. | It supplies metadata, not a local executor. Replace URL-as-ID/token overloading with an explicit stable repository binding and separate grant reference. Resolve the actual branch rather than assuming `main`. |
| [Local clone manager](../products/shipwright/backend/integrations/storage/git_clone_manager.py) / [Git storage](../products/shipwright/backend/integrations/storage/git_local.py) | Checkout lifecycle and file-access separation. | Existing manager hard-resets disposable caches, injects credentials into clone URLs and logs command errors. Do not use it unchanged on user worktrees or copy its credential transport/logging. |
| [Storage interface](../products/shipwright/backend/integrations/storage/base.py) | Capability-based file access/version references. | Prefer configured Django storage for SDLC artifacts; do not import another vault or transfer its provider defaults. Preserve SDLC ACL/retention semantics. |
| [Conduit GitHub service](../products/conduit/backend/integrations/services/github.py) | Branch/SHA, PR lookup/create and comment request shapes. | It depends on Conduit's integration model and builds credential-bearing clone URLs. Adapt the boundary; do not import its identity/storage model into SDLC. |

A local Git adapter must validate the explicit repository/allowed root, preserve dirty
user worktrees and operate on isolated approved worktrees for writes. Protect against
path/option injection, hooks, unsafe Git configuration and unapproved network protocols;
do not invoke arbitrary Git arguments supplied by an agent. Credentials use existing
credential helpers or restricted channels, never URLs/argv/logs. Tests use disposable
repositories, not resets or cleanup of a user's checkout. A filesystem path is not
proof of repository identity or execution authorization.

Do not extract a generic integration library merely to share a class name. Reuse or
adapt the narrow provider boundary in its owning package once the SDLC use is tested;
keep host-specific account models and permission checks outside generic clients.

## 6. Implementation order

The companion plan starts with **M0: usable internal work tracking**, now implemented,
before approval/execution M1. M0 includes UI/API, default-path tests, installed-wheel
checks and real PostgreSQL concurrency/crash tests, not just database models.
Exercise sync contracts with a deterministic test adapter before connecting providers;
only implemented adapters advertise sync. M2 adds internal QA/review evidence and a
local-Git path. M3 supplies selected real connectors and transfer/sync commands. M4
integrates Studio and verifies an opted-in pilot. No host setting or data is changed
by accepting this design; features beyond the documented M0 tracker do not exist yet.
