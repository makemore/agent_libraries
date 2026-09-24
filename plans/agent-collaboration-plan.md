# Agent collaboration and real-world endpoints: plan

Status: **steps 1–2 (core + messaging services) implemented locally** in
`packages/python/django_agent_workspace` (26 isolated tests passing). Decision 9.1
settled: agent identity lives in the runtime (section 4), implemented locally in
`agent_runtime_core.identity` and `django_agent_runtime.identity` (unreleased).
Workspaces are independent of `AgentSystem` (9.2 default). Everything else below
is still a proposal.
Pattern to follow: `packages/python/django_agent_sdlc` (independent, opt-in Django
package; supported service layer; scope ACLs; idempotent writes; Postgres for
concurrency; host callbacks for anything with cost or external effect).

## 1. Goal

Let groups of agents (and the humans who own them) collaborate through:

1. **Messaging**: team channels, direct messages between agents, threads, mentions.
2. **Shared data**: agents can define tables and read/write rows inside the main Postgres.
3. **Documents**: a small "Notion": pages, nested pages, revisions, links.
4. **Files**: shared and per-agent file spaces.
5. **Later, real-world endpoints**: an agent can own an email address, phone number
   or similar and act through it, within limits.

## 2. What already exists (verified in code)

| Existing piece | Where | Relevance |
| --- | --- | --- |
| `AgentDefinition` (slug, `owner` user FK, `is_public`) | `django_agent_runtime/models/definitions.py` | Agents have an owner but **no identity of their own** to hold permissions or be a message sender. |
| `AgentTool` (builtin / function / subagent) | same file | How new capabilities (post message, query table, write page) get attached to an agent. |
| `AgentSystem` + `AgentSystemMember` (roles, `shared_knowledge` JSON) | same file | Existing "team" grouping. A natural parent for a shared workspace, but shared knowledge is static JSON, not a collaborative store. |
| `EventBus` / `Event` | `django_agent_runtime/runtime/events/base.py`, `agent_runtime_core/events/` (memory, redis, sqlite) | Streams events **per run** to the UI. Not addressed agent-to-agent messaging and not durable inbox semantics. Useful for live UI updates only. |
| Outbound webhooks (`AgentWebhookSubscription`, HMAC-signed) | `django_agent_runtime/models/webhooks.py` | Outbound only. No inbound channel (email/SMS) handling exists. |
| Managed MCP connections and grants, BYOK encryption, run-scoped `MCPGatewayLease` | `django_agent_runtime/models/mcp*.py`, `docs/docs/setup/managed-mcp.md` | The right way to expose the new capabilities to agents as tools, with per-run, revocable authority and encrypted provider secrets. |
| File processing (`agent_runtime_core/files/`), `AgentDefinition.file_config` | runtime core | Handles uploaded files for a conversation, not a shared persistent file space. |
| SDLC scopes, grants, notes with revisions, activity, idempotency receipts | `django_agent_sdlc` | Proven pattern for ACLs, revisions and retries. Reuse the approach, not the tables. |

Nothing for email, SMS, phone or Twilio exists in the Python packages today.

## 3. Package recommendation

**Two packages, not one and not five.**

### `django_agent_workspace` (new): collaboration core
One package because messaging, data, docs and files all need the **same** identity,
membership, permission, activity and quota model. Splitting them would duplicate that
model or create a web of cross-package dependencies. Inside the package keep separate
Django apps so hosts enable only what they want (same idea as SDLC core + `delivery`):

| App | Contents |
| --- | --- |
| `workspace` (core, required) | Workspaces, members (agent or human principals), roles, activity log, quotas, service layer. |
| `workspace.messaging` | Channels, DMs, threads, mentions, per-principal inbox and read cursors. |
| `workspace.data` | Agent-defined tables and rows. |
| `workspace.docs` | Pages, hierarchy, revisions, links. |
| `workspace.files` | File tree metadata; blobs in Django storage. |
| `workspace.tools` | MCP / `AgentTool` adapters exposing the services to agents. |

### `django_agent_channels` (new, later): real-world endpoints
Separate because it has a very different risk profile: external providers, real money,
legal/compliance duties, untrusted inbound content and irreversible outbound actions.
It depends on the workspace core for identity and inbox delivery, never the reverse.

### Optional later: SDLC link
SDLC work items could reference workspace pages/channels. Keep it one-way (SDLC
optional extra points at workspace); no FK from workspace core to SDLC.

## 4. Foundation: agent identity (do first)

Everything else needs an answer to "who is acting?". Decided and implemented:

- **One identity, owned by the runtime.** `agent_runtime_core.identity` defines the
  canonical principal string (`agent:<AgentDefinition UUID>`, `user:<pk>`; other
  existing kinds such as `team:` stay valid). `django_agent_runtime.identity`
  resolves them to active rows. Workspace, SDLC, channels and MCP grants all key on
  the same strings; no package keeps its own identity table.
- Runs carry `ctx.identity` (agent, initiator, acts_as), derived from durable rows
  only. Default `RUN_ACTS_AS = "initiator"` keeps today's behaviour; `"agent"`
  makes MCP checks use the agent's own `MCPPrincipalGrant` rows.
- **An agent acts with its own grants, not its owner's.** Owning or starting an
  agent gives it no workspace access; a manager must add it, and the host callback
  `WORKSPACE_CAN_ADD_AGENT` must approve. Handles (`@researcher`) and roles stay in
  the workspace package, per workspace.
- Workspace tools (step 5) take the actor from `ctx.identity.agent`.

## 5. Component designs

### 5.1 Messaging ("team chat" and DMs)
- Tables: `Channel` (public/private within a workspace; DM = 2-member private channel),
  `Message` (author principal, body, thread parent, mentions), `ChannelMember`,
  `ReadCursor`, `InboxItem` (per recipient: mention, DM, reply).
- Postgres is the bus: durable rows are the source of truth, `LISTEN/NOTIFY` only
  wakes listeners. No Redis/Kafka needed at first. The existing `EventBus` can push
  live updates to the UI.
- **Agents are not always running**, so delivery = "wake the agent": an inbox item
  can trigger a new run of the recipient agent, through a host-supplied callback.
- Required guard rails: loop/ping-pong detection (max auto-replies per thread and per
  hour), per-principal rate limits, run budget via host callback (same rule as SDLC:
  no built-in budget provider, missing callback = blocked), and a human "pause workspace".
- Agent tools: `send_message`, `read_channel`, `list_inbox`, `mark_read`, `search_messages`.

### 5.2 Shared data (agents creating schemas and using them)
Two levels; build the first, keep the second as an explicit later option.

**Level 1 (recommended default): JSONB "collections", like Notion databases.**
- `Collection` (name, column definitions as validated JSON: text, number, bool, date,
  select, relation, principal), `Row` (JSONB data, revision, author).
- "Schema creation" = creating/altering a collection's column definitions through the
  service layer, with validation, revisions and an activity trail. No raw DDL.
- Queries through a small structured filter/sort API (JSON), translated to ORM with
  GIN indexes. No raw SQL from agents.
- Safe in the main Postgres, uses normal migrations, trivial to back up, permission-checked.

**Level 2 (optional, later, needs approval): real Postgres schema per workspace.**
- A dedicated Postgres schema and a low-privilege role per workspace; agents run
  real `CREATE TABLE` / SQL only inside it.
- Needs: `statement_timeout`, row/size quotas, no access to `public` or other schemas,
  connection limits, DDL audit, and RLS or role separation. Much higher risk (locks,
  runaway queries, schema drift, backups). Only if Level 1 proves too limiting.

### 5.3 Documents (the "Notion" part)
- `Page` (workspace, parent page, title, Markdown body, revision, archived),
  `PageRevision`, `PageLink` (backlinks), optional per-page ACL narrowing.
- Optimistic concurrency with `expected_revision` (as SDLC does) so two agents cannot
  silently overwrite each other; conflicts return 409 and the agent re-reads.
- Collections can be embedded in pages later.
- Agent tools: `create_page`, `read_page`, `update_page`, `search_pages`, `list_children`.

### 5.4 Files
- `FileNode` tree (folder/file, path, size, content type, checksum, version) in Postgres;
  blobs in the host's Django storage (GCS/S3/local). Not a real mounted file system.
- Spaces: one shared space per workspace plus a private space per principal.
- Quotas per workspace/principal, type/size limits, versioning, soft delete.
- Later: sync a space into a sandbox for execution (conduit / execution providers),
  as an explicit, separately approved step.
- Agent tools: `list_files`, `read_file`, `write_file`, `move`, `delete`.

### 5.5 Cross-cutting rules
- All writes through services: keyword-only, idempotency keys, activity entries,
  permission rechecks, Postgres row locks (copy SDLC's contract).
- Everything an agent reads from another agent is **untrusted data**, never instructions
  (prompt injection between agents is a real risk).
- Quotas and retention per workspace; redaction for privacy.
- Studio UI: channel view, page view, collection grid, file browser, activity feed,
  "pause workspace" button. Mount like SDLC (`django_agent_workspace:home`).
- Tests: isolated settings, Postgres for concurrency, default-path tests plus named
  exception tests (per `AGENTS.md`). No persistent demo seeding without approval.

## 6. Real-world endpoints (`django_agent_channels`)

### 6.1 Model
- `Endpoint` (kind = email | sms | voice | …, address, provider, owning principal,
  status), `EndpointPolicy` (who may contact it, allowed recipients, send limits,
  approval mode), `ExternalMessage` (in/out, provider IDs, status, raw payload ref),
  `ProviderConnection` (credentials via existing BYOK encryption).
- Inbound messages become `InboxItem`s for the owning agent (reuses 5.1 wake-up path).
- Outbound goes through a durable outbox with idempotency keys so retries never
  double-send.

### 6.2 Email
- Goal: each agent has what feels like its own Gmail (threads, replies, labels,
  drafts, attachments, search), shown as the agent's inbox in the workspace.
- First provider: AgentMail (inbox-per-agent API, custom domains, signed webhooks).
  Behind a provider interface so Postmark/SES/Mailgun can replace it.
- Default: inboxes on a host-configured domain or subdomain.
- Verify inbound webhook signatures; limit attachments; thread by Message-ID.

### 6.2a Agent-owned domains (autonomous, owner opt-in)
- An agent with the **domains** capability runs the whole flow itself, with no
  per-action approval: search and buy a domain (registrar API), create DNS, add
  the domain to the email provider, write its DKIM/SPF/DMARC records, wait for
  verification, then create inboxes.
- The owner turns this on per agent and sets its **spending limit** (one-off
  purchases plus renewals, per month). That limit is the control: a purchase or
  renewal that would exceed it is refused. Optional owner settings: allowed domain
  endings, max price per domain, auto-renew on/off.
- Providers behind interfaces: registrar (e.g. Route 53 Domains, Porkbun,
  Namecheap, Name.com, Gandi) and DNS (e.g. Cloudflare, Route 53). The host
  account holds registrar/DNS/email keys (encrypted, never given to the agent).
- Every step is recorded (domain, price, agent, time) and idempotent, so a retried
  run never buys twice. Per-agent and global off switch.
- Domains are registered to the host's account; the host is the legal registrant.
  New domains start with no email reputation, so the send path ramps volume on
  them automatically.

### 6.3 Phone and SMS
- Provider for numbers, SMS and voice (e.g. Twilio, Vonage, Telnyx).
- SMS first; voice needs real-time speech in/out and is a larger project.
- Registration duties before sending: e.g. US A2P 10DLC, UK sender rules, per-country
  number requirements.

### 6.4 Controls
- **Autonomy is the owner's choice per agent.** An owner can let an agent send,
  buy and configure without approval; approval-first remains available as a
  per-agent setting for owners who want it.
- Spending limits per agent (and optionally per workspace) through a host callback;
  every paid action checks them first.
- Rate limits, recipient allowlists, kill switch per endpoint and globally.
- AI disclosure where required; consent, opt-out (STOP) handling, record keeping
  (GDPR/PECR in UK, TCPA/CAN-SPAM in US).
- Inbound content is untrusted: never lets an outsider grant permissions, change policy
  or trigger payments.
- Full audit trail of every inbound and outbound message.

## 7. Build order

| Step | Deliverable | Depends on |
| --- | --- | --- |
| 1 | Workspace core: principals, workspaces, members, roles, activity, service contract, tests | decisions 9.1–9.2 |
| 2 | Messaging: channels, DMs, threads, inbox, UI; agent tools via MCP | 1 |
| 3 | Agent wake-up on inbox, loop guards, budget callback | 2, host budget callback |
| 4 | Docs/pages | 1 |
| 5 | Data collections (Level 1) | 1 |
| 6 | Files | 1, storage decision |
| 7 | Studio integration (like SDLC: Dockerfile, mount, deployment notes) | 2–6 |
| 8 | Channels package: email (AgentMail), inbound to inbox, outbound per the agent's autonomy setting | 3, AgentMail account + default domain |
| 8a | Agent-owned domains: registrar + DNS providers, spending limits, full autonomous flow | 8, registrar/DNS accounts |
| 9 | SMS pilot, then voice | 8 |
| 10 | Optional: Level 2 real Postgres schemas, sandbox file sync, SDLC links | separately approved |

Steps 4–6 are independent of each other and can run in parallel after step 1.

## 8. Risks

- Agents talking to each other can loop and burn money fast: loop guards and hard
  budget caps are required from step 3, not added later.
- Agent-created data and pages can hold prompt injections aimed at other agents.
- Real Postgres DDL by agents risks locks and outages in the main database (why Level 1 first).
- Real-world endpoints and purchases are real money and legal actions in the host's
  name; the owner-set spending limit and off switch are the controls.

## 9. Decisions needed

1. **Identity**: decided: runtime-level identity shared by all packages (section 4).
2. **Workspace parent**: tie a workspace to an `AgentSystem`, or keep workspaces
   independent with members added explicitly (recommended: independent, optional link)?
3. **Shared data**: confirm JSONB collections first, real SQL schemas later/never.
4. **Wake-up and budget**: who supplies the budget-cap callback, and what caps?
5. **File storage**: which bucket/storage backend for Studio.
6. **Endpoints**: decided: AgentMail for email; agents may run the full domain
   flow autonomously when their owner enables it, limited by spending caps.
   Still needed: default domain, registrar and DNS accounts, SMS/voice provider,
   countries.
