# SDLC domain and approval contract

Status: **Delivery/approval draft; M0 tracker implemented separately, no live enablement.** 2026-09-17.
Companion: [implementation slices and acceptance tests](sdlc-implementation-plan.md).
Local-first revision: [internal capabilities and provider sync](sdlc-local-first.md).
This remains a proposed delivery/approval contract, not evidence of workflow APIs.
The owning package now has [implemented M0 contracts](../agent/django_agent_sdlc/docs/contracts.md)
and [installation/verification evidence](../agent/django_agent_sdlc/README.md).
This cross-package design remains here until its later milestones are implemented.

## 1. Scope and package boundary

Build `django-agent-sdlc`, containing the `django_agent_sdlc` Django app, as the
first consumer of the [optional ACE bridge](../agent/django_agent_runtime/docs/ace-integration.md).
The core checkout is `agent/django_agent_sdlc`; its optional delivery app is not yet implemented.

- ACE owns durable process state, inboxes, activities, retries and timers.
- Runtime owns agent execution, conversations, history, tools and credential grants.
- SDLC owns internal work tracking, delivery intent, revision-specific evidence,
  approvals and integration policy. External issue trackers are optional.
- The core app's issues/notes/checklists run without runtime, ACE or Studio. Put
  delivery models/workflows and their migrations in optional `django_agent_sdlc.delivery`;
  the delivery extra/app enables the runtime/ACE dependency, not ordinary local CRUD.
- Optional `django_agent_sdlc.sync` uses ACE for provider transport without requiring
  delivery/runtime. Optional apps own links to core, never mandatory core FKs to them.
- Optional `django_agent_sdlc.studio` URLs/views/templates use Studio's shell.
  The SDLC core services, models and workflow must import without Studio installed.
- The host explicitly installs apps, mounts namespaced URLs and configures adapters.
  No plugin discovery, monkey-patching, new permission framework or workflow DSL.
  Neither generic Studio nor runtime imports SDLC. A small authorized navigation
  hook is a later Studio change, not an existing plugin API.

First workflow: specification → human approval → engineering → CI/review → QA →
human release decision. Its successful result is **release authorized**, not
merged, deployed or released. Humans retain merge/release control. Multi-repository
dependency graphs, autonomous release, and policy scripting are outside v1.

## 2. Existing contracts we can actually reuse

| Existing source | Consequence for SDLC |
| --- | --- |
| [Bridge services](../agent/django_agent_runtime/integrations/ace/services.py): `bind_workflow`, `dispatch_agent`, `publish_signal` | Supported services reject rebinding a workflow's execution principal; dispatch uses that principal, not a user ID supplied in metadata. This is not protection against direct privileged ORM/SQL mutation. |
| [Trusted run services](../agent/django_agent_runtime/services/runs.py): `submit_run`, `read_run`, `cancel_run` | Keep validation, ownership, quotas, history and cancellation semantics; returned ORM objects are not public API projections. |
| [Run serializer](../agent/django_agent_runtime/api/serializers.py): `AgentRunCreateSerializer` | `system_version_id` is a supported explicit field; ownership and execution-target metadata are server-managed. |
| [Runtime registry](../agent/django_agent_runtime/runtime/registry.py): `get_runtime_for_system_version` | Loads the current system entry agent's pinned revision from the selected version. It is not a universal immutable execution capsule. |
| [Runtime execution targets](../agent/django_agent_runtime/runtime/execution_targets.py) | Stable agent/directory targets have supported server-side binding and reauthorization. They do not prove filesystem isolation or code revision pinning. |
| [Managed MCP models](../agent/django_agent_runtime/models/mcp_connections.py) | Reuse `MCPServerConnection`, `MCPPrincipalGrant` and the credential vault when using MCP; `MCPRunMount` is audit evidence, not authority. |
| [Studio workspace adapter](../agent/django_agent_studio/workspace_backends.py) | Existing host projects use immutable namespaced keys; SDLC need not introduce a second organization or membership system. |
| [Studio access rules](../agent/django_agent_studio/WORKSPACE.md#ownership-and-access) | A project viewer cannot automatically read an execution principal's runtime history. Workspace revocation alone is not runtime/credential revocation. |
| [Runtime definitions](../agent/django_agent_runtime/models/definitions.py) | Agent revisions/system snapshots can be referenced. `SpecDocument` describes agent behavior, not a ready-made delivery approval ledger. |

All entity and service names below are **proposed SDLC names**, not current APIs.

## 3. Scope, actors and authority

### Host integration

`DeliveryScope` binds `(namespace, project_key)` and an explicit authorization backend.
For an existing Studio/host project it is not a project clone: resolve its names,
membership and action permissions through that backend. The built-in `internal`
backend permits standalone scopes with explicit local grants when no host project is
selected, as specified in the local-first contract. Do not copy host memberships into
local grants. No importable model label or arbitrary queryset comes from request data.
Keys and their backend identity must be stable and must never be reassigned or reused.

The proposed adapter contract supplies authorized scope enumeration, per-action
authorization, trusted actor classification, role/grant resolution, and a documented
DB-local ACL lock/revocation protocol. Omitted new-scope configuration selects the
built-in backend; missing configured adapters, unknown scopes and lookup errors deny
access. Changing a default never retargets existing scopes. The optional Studio adapter
reuses `WorkspaceBackend` to resolve projects, then applies SDLC-specific permissions;
local or host editor/manager is not approver. Unconfigured delivery approval policy
blocks delivery execution, not the creation and discussion of ordinary work items.

### Four identities, never one overloaded user ID

1. **Requester:** authenticated human or permitted automation opening delivery work.
2. **Execution principal:** active Django user bound to the ACE workflow; owns its runs.
3. **Agent role:** analyst, engineer or verifier configuration assigned to that principal.
4. **Approver:** authenticated, currently eligible human acting as themselves.

Provider installation/bot identity is a separate integration identity. Link it by
stable provider IDs and grant references; display names, emails and supplied metadata
are not proof of identity. Account provisioning/OAuth consent stays with the host.
Agents may recommend, but cannot impersonate humans or satisfy human approval quorum.
Trusted host account classification must distinguish automation from human accounts;
`is_authenticated`, `is_staff` and a client-supplied `actor_kind` do not establish this.

**Current bridge limit:** a single workflow cannot switch execution users by role.
The provider-free first slice exercises one explicitly selected execution principal
with separate agent roles and separate human approvers. This is not per-role identity
isolation. Do not silently adopt shared credentials for a live organizational pilot.
If distinct role users are required, implement and validate separately bound role
workflows plus durable parent/outcome correlation first; never mutate a binding.

| Action | Required authority, in addition to active account and scope access |
| --- | --- |
| View/list | Host delivery-view permission; filter before counts and pagination. |
| Open/revise/assign | Specific delivery-write/assignment permission; assignments confer no tool grants. |
| Execute | Bound principal, active attempt/request, approved role configuration, current runtime/tool/target grants. |
| Record evidence | Trusted evidence producer for this exact subject; automation results are not human votes. |
| Approve/reject | Required human role, no forbidden authorship/executor conflict, current review and policy. |
| Cancel/retry/recover | Explicit operation permission; recovery cannot manufacture approval or erase evidence. |

No implicit superuser bypass. A human reviewer may see an authorized structured
evidence projection without receiving runtime-owner credentials. Full transcripts
require the existing authorized read/share path; otherwise show unavailable, not an
impersonated `read_run` response. Membership never grants raw prompt/tool access.

## 4. Proposed data model

UUIDs are internal primary keys. Work items keep their UUIDs when a tracker is linked
or superseded; tracker state is separate from delivery state. External identity uses
provider-instance/installation/object IDs, never a mutable URL or branch name alone.
Local Git repositories use explicit stable repository bindings, not paths as identity.
Delivery-domain rows carry scope through their delivery/attempt relationship;
core tracker/sync rows can belong directly to a scope with no delivery. Every service
validates cross-row scope consistency.

| Entity | Key fields and invariants |
| --- | --- |
| `DeliveryScope` | Unique namespace/project key and pinned authorization backend. Built-in scopes use local grants; host-backed scopes never duplicate a membership roster. Delivery policy/assignments are separately enabled. |
| `PolicyRevision` | Scope, revision, schema version, explicit gate requirements, role/configuration requirements and expiry policy. Published content is immutable; absent configuration does not allow execution. |
| `RoleAssignment` | Scope, role, revision, principal ID, agent ID/key, approved runtime release/configuration fingerprint, grant references. Replace rather than retarget an accepted assignment; revocation is separately recorded. |
| `Delivery` | Scope, requester, title, current specification and active attempt references. No independently editable workflow-status column. Idempotency scoped to requester/scope/open command. |
| `SpecificationRevision` | Delivery, monotonic revision, author, acceptance criteria, content digest and protected content or authorized artifact reference. Never edit an accepted revision in place. |
| `DeliveryAttempt` | Delivery/attempt number, specification, policy and role-assignment references, execution principal, unique ACE workflow reference; current candidate and monotonic authorization generation. At most one active attempt per delivery. |
| `WorkRequest` | Attempt, stable business step key, role assignment, expected principal/generation, immutable input references and prerequisite gate-use references. Unique attempt/step key. Technical retries preserve this record. |
| `CandidateRevision` | Attempt/revision, spec, repository binding, exact head/base commit IDs, target ref and relevant artifact/environment digests. Branch/PR alone is insufficient. Rework creates a new candidate. |
| `EvidenceRecord` | Exact spec/candidate, evidence kind, producer identity, provider job/run/attempt IDs or runtime run ID, typed verdict, observed/source times, digest and protected artifact references. Immutable facts; corrections/revocations are new records. |
| `GateReview` | Attempt, kind, generation, subject and evidence manifest, policy revision/digest, opened/expires times. Freeze the entire reviewed subject; one active review per attempt/kind/generation. |
| `Decision` | Review, actor ID/classification, action, bounded rationale reference, accepted time and idempotency key. Append-only approvals/rejections/change requests/withdrawals; withdrawal references an earlier decision. |
| `GateUse` | Review, contributing decision IDs, exact action key/subject/generation and authorization receipt time. Unique review/action key; proves past authorization, not permanent future permission. |

Use FKs/checks/unique constraints where practical; cross-scope joins and immutable
state transitions also require service validation and concurrent tests. Restrict
admin/API edits of published records. ORM-level append-only conventions are not
tamper-proof storage against a privileged database operator.

The core work-item/note/comment/checklist model, capability binding revisions, stable
external links and sync checkpoints are defined in the [local-first contract](sdlc-local-first.md).
Internal work can exist without a `Delivery`; explicit links connect the two. Closing
an external or internal issue never becomes a human vote or a workflow terminal result.

`RepositoryBinding` arrives with local Git support; `ProviderDelivery` and
`ExternalOperation` arrive with external adapters, not as a new credential store.
Repository bindings represent exact local repositories or immutable provider repository
IDs within a scope. Provider receipts deduplicate verified deliveries. External-operation
records track intended mutations, acknowledgement and uncertain outcomes separately
from ACE activity attempts. Tracker sync does not require a delivery/attempt FK;
delivery associations belong to the optional integration. Details are in the
[implementation plan](sdlc-implementation-plan.md).

### Content, defaults and retention

ACE state/events/receipts carry opaque IDs, generations, schema-defined outcomes and
necessary digests, not spec bodies, prompts, outputs, credentials or raw webhook JSON.
Runtime remains the transcript authority; do not copy its history into an SDLC ledger.
Structured evidence is deliberately created domain data with its own authorized
projection, provenance and retention, not an automatic archive of model output.

Protect spec/rationale/artifact content with explicit ACL and retention policies before
live persistence. A digest does not grant access or prove an artifact is still present.
Required purged/redacted/unreadable evidence becomes unavailable and cannot pass a gate;
no fallback to cached transcript copies. Keep minimal retry/audit tombstones while
work can retry, subject to an approved cleanup policy; do not invent indefinite retention.
Never copy signed URLs, secrets or auth headers into references, logs or evidence.
Agent storage mode stays blank and conversation history initialization stays with
runtime. Preserve deliberate host overrides and previously persisted choices.

## 5. Revision-bound approval contract

### Exact subject, not a green badge

A gate's canonical subject includes the delivery/attempt, gate kind, authorization
generation, specification ID/digest, applicable candidate/repository/head/base/target,
artifact/environment identities, frozen evidence manifest, policy revision/digest and
role-configuration references. The subject schema is versioned. Canonical digests aid
comparison; explicit typed IDs and equality checks remain authoritative.

The UI must display this subject and submit its review ID, expected generation and
subject digest. It cannot submit just a delivery ID and `approve=true`. Missing,
unknown, stale or mixed-scope fields fail before any decision is persisted.

**Proposed v1 policy, requiring sign-off:** one independent human at specification
and release gates, with explicit approver roles; neither the subject's recorded
author nor execution principal can approve it. Multiple accounts for one mapped
human count once. No quorum reduction by an agent; no risk-based automatic bypass.
The host must explicitly choose approval lifetime and required CI/review/QA evidence.
Unconfigured lifetime/evidence rules disable gate opening, not validation.

CI/review evidence includes producer identity, check name, exact candidate, attempt
and terminal result; only explicitly configured successful conclusions pass. Missing,
pending, skipped, cancelled, unknown or unauthenticated results do not mean success.
QA records acceptance-criterion coverage and a typed verdict. Runtime `succeeded`
alone is not proof of correct code, CI success or QA acceptance. Greptile review may
provide trusted-source evidence, but never stands in for an authenticated human vote.

### Acceptance and consumption are different

Proposed `record_decision` takes the authenticated actor, review ID, expected
generation/digest, action and idempotency key. It validates current scope access,
human eligibility, conflict-of-interest rules, policy, evidence visibility and review
liveness under the lock contract below. It records the immutable decision and calls
bridge `publish_signal` in the same transaction. The bridge signal carries a decision
reference and actor ID, not rationale, authority claims or a bearer capability.
Signal publication also invokes the host callback with `signal:<name>`; SDLC must
validate that actor's referenced decision, not require them to be the execution user.

An identical authorized retry returns the same decision; the same key with different
actor/subject/action conflicts. Idempotency never bypasses current read/action access.
One human contributes at most one active approval to a review. Rejection or change
request closes that review; reopening creates a new generation/review rather than
counting an earlier vote. Withdrawal is append-only and invalidates dependent uses.

Proposed `authorize_gate_use` recomputes eligibility before a protected action:
current attempt/generation and subject, unwithdrawn eligible votes, quorum, expiry,
unrevoked policy/configuration/grants, required evidence status and availability.
It creates an action-bound `GateUse` receipt. Reusing a receipt requires those checks
again; the receipt is not a reusable capability and possession of its ID grants nothing.
Losing a contributing approver's eligibility invalidates the unconsumed approval;
historical acceptance remains visible with its then-valid provenance.

### Invalidation and races

- Spec changes supersede the attempt and require a fresh specification review.
- Head/base/target/artifact or required evidence changes create a new candidate/review
  generation; never transfer approvals because the PR number stayed the same.
- Policy/role changes, approval withdrawal, evidence retraction and relevant access
  revocation invalidate outstanding protected work. Policy replacement applies to new
  attempts; continuing an old policy requires explicit authorization, never silent
  migration. Revoking an old policy prevents its pending actions.
- Invalidation updates authoritative domain state immediately in its transaction and
  emits a durable notice. Delayed notices cannot leave old requests authorized.
- If an action commits before invalidation, retain that fact; block subsequent actions
  and request cooperative cancellation for active work. Do not claim rollback of an
  already-started tool call, remote process or external effect.

**Concrete example:** release review R approves candidate C at head H1 with evidence
E1. A force-push to H2 advances the generation. A late R approval or a queued action
using R is rejected, even if ACE has not yet consumed the H2 notice. A late H1 check
result remains H1 evidence; it cannot make H2 releasable.

## 6. Durable workflow and service contracts

| ACE domain phase | Exit requirement |
| --- | --- |
| `specification` | A recorded specification revision ready for review; analyst output is only a proposal. |
| `awaiting_spec_approval` | A current, action-bound gate use authorizes engineering. |
| `engineering` | A candidate is recorded from validated engineering output, not inferred from run status. |
| `awaiting_checks` | Required authoritative evidence matches the candidate; otherwise wait or request rework. |
| `qa` | Authorized verifier work produces validated criterion-level evidence. Failure requires explicit rework. |
| `awaiting_release_approval` | Current evidence and eligible human decisions authorize this exact release subject. |
| terminal result | `release_authorized`, `rejected`, `cancelled`, or `superseded`; never label authorization as deployment. |

These are ACE domain-state values, not a second writable status field in SDLC.
Infrastructure `BLOCKED`/retry state remains ACE's. UI projections must also show
current domain invalidation and the reason work is waiting; an old phase is not an
authorization decision. There is no automatic infinite engineering/QA retry loop.
Business rework requires an authorized new request/key and a policy-defined budget;
technical retries keep the original request and bridge run.

Workflow transitions remain deterministic: no ORM reads, current-time policy queries,
network calls or credential access. Domain activities evaluate mutable authority and
produce minimal durable receipts (reference, generation, subject digest, outcome).
A decision signal is a wake-up to evaluate a gate, not permission to advance blindly.
Transitions tolerate duplicates, out-of-order notices and agent outcome before dispatch
acknowledgement. Old-generation receipts cannot reset a newer phase.

Final authority is in the services: bridge `AGENT_RUNTIME_ACE_AUTHORIZE` checks the
current WorkRequest/gate prerequisites at dispatch and execution; the resolver loads
only that accepted request's immutable input references. A late ACE receipt may cause
a stale activity to be scheduled, but cannot authorize a new run or protected mutation.
Reconciliation also cancels now-invalid active work and reports unresolved cancellation.
Tool/external adapters must enforce current grants at their own dispatch boundary.

SDLC supplies the domain checks for the existing `bind`, `dispatch`, `execute` and
`signal:<name>` callbacks; the generic bridge cannot infer them. Bridge recovery checks
`execute` before nudging queued work. SDLC user cancellation requires its own operation
permission before runtime cancellation; bridge safety cleanup must still be able to
cancel a verifiably owned run after the original principal loses permission.

### Transactions and lock order

Use the bridge's single primary `default` database, not a cross-database outbox.
For existing workflows, acquire `WorkflowRun`, then binding, then the documented
host ACL/domain parent locks and subordinate rows, then runtime locks. Use stable
ordering for multiple same-kind domain rows. Never hold a domain lock and then call
the bridge to acquire an existing workflow lock. The decision transaction takes the
workflow/binding locks first, so publishing its signal reenters those same locks.

Host ACL mutation must use the same ACL parent lock, commit revocation, and schedule
workflow cancellation/reconciliation afterward in a separate transaction; it must not
lock ACL first and then enter ACE. Do not blindly call Studio `get_project(lock=True)`
inside callbacks until the host's complete lock graph has been verified. Incompatible
ACL protocols block that host adapter's rollout; they are not permission bypasses.

Starting an attempt serializes on a delivery with no existing bound workflow, creating
ACE run, attempt and binding atomically. A duplicate start discovering an existing
workflow must leave the discovery transaction and enter the existing-workflow lock
order before doing further work. Starting a successor first fences the prior attempt;
never take an old workflow lock while holding the new attempt's domain locks.

Proposed commands: `open_delivery`, `revise_specification`, `start_attempt`,
`record_candidate`, `record_evidence`, `open_gate_review`, `record_decision`,
`authorize_gate_use`, `request_rework`, `cancel_attempt`. Each uses explicit actor,
scope, expected revision/generation and a durable idempotency key where it writes.
Services, HTTP endpoints, activities and management commands share these write paths.
GET/list/preview endpoints have no mutation side effects. Session writes require CSRF;
webhook authentication is separate and cannot call the human-decision API as a user.

## 7. Runtime configuration and execution limits

An agent key or current `AgentVersion` is not immutable approval material. The proposed
role assignment records an approved release reference and protected configuration
digest. Use supported `system_version_id` where appropriate; validate the expected
entry-agent identity and pinned revision at acceptance and execution, not just the
version UUID. Do not invent a per-run `agent_version_id` serializer field.

Live tool grants, host hooks, MCP configuration and remote execution still have their
own lifecycles. A fingerprint check alone cannot prove isolation from concurrent
configuration edits. Before live engineering, prove the relevant loader and outbound
adapter use the approved configuration/target at the actual execution boundary; if
that needs a runtime extension, implement and test it explicitly. No fallback to a
different active agent/model, no copying unrestricted snapshots into run metadata.

Remote work also needs an approved isolated working directory, exact code checkout,
command/network policy and bounded resource use. Runtime's directory binding alone
does not supply these. The provider-free slice must not be presented as validation
of a shell executor, real repository writes or safe production deployment.

## 8. Decisions needed before a live pilot

Recommended starting choices, **not silently enabled defaults**:

1. One explicitly selected local Git repository or hosted repository. Internal issues
   are the starting option; GitHub/Linear or other implemented adapters may be linked,
   mirrored or made authoritative for selected tracker fields through an approved
   transfer. Code/check authority is selected separately; SDLC retains approval authority.
2. Independent human specification/release gates; select eligible users/roles,
   acceptable evidence sources, evidence/approval expiry and rework budget.
3. Choose shared execution principal explicitly or require distinct role principals.
   The latter adds separately bound workflow coordination before pilot enablement.
4. Select host scope/ACL adapter, agent release configurations and restricted execution
   environment; verify revocation and locking contracts end-to-end.
5. Approve content/evidence retention and sharing rules, provider installations/grants
   and protected branches. No package installation grants external permissions.
6. For any provider sync, approve exact destination, field ownership, status/identity
   mapping, content export and audience restrictions. Disconnect/reconnect and later
   provider changes preserve stable IDs but never bypass source ACLs or retention.

The next coding milestone can use isolated test scopes and explicit test policies;
none of these unresolved live choices needs to be filled with a durable demo override.
