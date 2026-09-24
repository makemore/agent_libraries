# django-agent-workspace

Shared workspaces where agents and humans collaborate. **Step 1–2 of
[the collaboration plan](../../../plans/agent-collaboration-plan.md)**: identity,
membership, activity and messaging. Docs/pages, data collections, files, agent
tools (MCP), UI/HTTP API and Studio integration are **not implemented yet**.

Local package only: not a git repository yet, not published, not installed in any host.

Requirements: Python >=3.11, Django >=5.2 and `agent-runtime-core` (identity format).
Install `django_agent_runtime` too (extra `runtime`) so agent principals resolve to
`AgentDefinition` rows; without it only users are active and agents are denied.

## What it provides

| Module | Purpose |
| --- | --- |
| `identity.py` | Workspace view of the shared runtime identity: principals are `agent:<AgentDefinition UUID>` or `user:<pk>` strings (`identity.for_user(user)`). No workspace identity table. |
| `models.py` | `Workspace`, `Membership` (viewer/member/manager + `@handle`), `Activity`, `IdempotencyReceipt`, `Channel`, `ChannelMember`, `Message`, `InboxItem`. |
| `services.py` | `create_workspace`, `list_workspaces`, `list_members`, `set_member`, `set_paused`, `list_activity`. |
| `messaging.py` | `create_channel` (public/private), `open_direct` (one DM per pair), `add_to_channel`, `list_channels`, `post_message` (threads, `@mentions`), `list_messages`, `mark_read`, `list_inbox`. |

Write only through these services. Direct ORM writes bypass authorization.

## Rules

- **Agents have their own identity, owned by the runtime.** The same
  `agent:<uuid>` key is used for MCP grants, SDLC and channels. An agent acts with
  its own workspace membership; its owner or the user who started its run gives it
  no access. Hosts pass an agent run's `ctx.identity.agent` as the actor.
- Non-members get `NotFound`, so they can't tell whether a workspace or channel exists.
  Deactivated users and principals are denied on every call.
- Creates take an `idempotency_key` (at most 128 characters). To retry after an
  uncertain result, reuse the same key and payload; reusing a key for different
  content returns `Conflict`. Membership changes use `expected_revision`.
- Messages are durable Postgres rows, ordered by a per-channel `seq`. Activity
  records metadata only, never message bodies.
- Message bodies are untrusted content from other principals, never instructions.

## Host settings (all optional)

| Setting | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE_CAN_ADD_AGENT` | unset → adding agents blocked | Dotted path/callable `(user=, agent=)` with principal keys; must return exactly `True`. The host checks the manager may bring that agent in (e.g. owns the `AgentDefinition`). |
| `WORKSPACE_ON_INBOX` | unset → no wake-up | Dotted path/callable `(inbox_item_ids=)`, run after commit. Use it to start a run for a recipient agent. **It must enforce the host's hard budget cap**; this package has no budget provider. |
| `WORKSPACE_AGENT_MESSAGES_PER_HOUR` | 60 | Per-agent posting limit. |
| `WORKSPACE_MAX_AGENT_STREAK` | 20 | Consecutive agent messages allowed in one thread/channel before a human must post. Stops agent-to-agent loops. |

Managers can **pause** a workspace (`set_paused`). While it's paused, agents can't
post or create channels; humans still can.

## Tests

Isolated in-memory settings; no host database, no network. From the workspace root:

```sh
.venv/bin/python -m pytest -q -p no:cacheprovider \
  -c packages/python/django_agent_workspace/pyproject.toml \
  --rootdir packages/python/django_agent_workspace \
  packages/python/django_agent_workspace/workspace_tests
```

SQLite covers behaviour only. Concurrent writers (row locks on workspace and
channel) still need a PostgreSQL test run, which hasn't been added yet.
