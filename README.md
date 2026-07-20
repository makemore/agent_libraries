# Agent Libraries

A **meta-repo** that ties together the agent platform's independent
repositories: Python/Django backend, mobile + web clients, Studio UI,
the chisel tool-builder framework, the parrot registry, and the docs
site.

## Known follow-ups (post-reorg)

After the folder reorganisation, one client-side helper script still
references the old stub-server path:

- `clients/scripts/start_stub_server.sh` — line 14 sets
  `STUB_DIR="$REPO_ROOT/clients/test-stub-server"`. That folder has
  moved to `test-harness/stub-server/` (and the previously-committed
  `.venv` inside it was purged, so the script's venv-detection branch
  will fall through to the system Python).

To finish the move, edit the file inside the `clients` sub-repo:

```bash
# 1. Update the path (and drop the venv block if you no longer want it):
cd clients
$EDITOR scripts/start_stub_server.sh
#    change:  STUB_DIR="$REPO_ROOT/clients/test-stub-server"
#    to:      STUB_DIR="$REPO_ROOT/test-harness/stub-server"

# 2. Sanity-check it resolves from the clients/ root:
ls ../test-harness/stub-server/server.py

# 3. Run it and commit inside the clients sub-repo:
./scripts/start_stub_server.sh        # should boot the stub on :$STUB_PORT
git -C . add scripts/start_stub_server.sh
git -C . commit -m "Point start_stub_server.sh at the moved test-harness path"
```

## One-shot checkout

```bash
git clone https://github.com/makemore/agent_libraries.git
cd agent_libraries
make checkout    # clones every sub-repo into the right relative path
make install     # editable-install the Python packages
```

`make checkout` is idempotent — re-running it skips any sub-repo that's
already present.

## Layout

```
agent_libraries/
├── Makefile                 ← `make checkout` / `make install`
├── scripts/                 ← meta-repo tooling (checkout, install, deploy, …)
├── plans/                   ← cross-cutting design docs & specs
├── assets/                  ← shared brand / UI assets
├── test-harness/            ← shared test fixtures and stub servers
├── docs/                    ← mkdocs source (own repo)
├── agent/                   ← backend Python packages (own repo per package)
├── clients/                 ← mobile, web, TS, Unity clients (own repo each)
├── chisel/, django_chisel/  ← tool-builder framework (own repo each)
└── parrot/                  ← agent version-control registry (own repo)
```

Every sub-directory that is its own git repo can also be worked on
independently. The meta-repo only holds the layout, scripts, and
shared collateral.

## Sub-repos

| Path | Repo |
|---|---|
| `chisel/` | https://github.com/makemore/chisel |
| `django_chisel/` | https://github.com/makemore/django_chisel |
| `parrot/` | https://github.com/makemore/parrot |
| `docs/` | https://github.com/makemore/agent-docs |
| `agent/agent_runtime_core/` | https://github.com/makemore/agent-runtime-core |
| `agent/agent_studio/` | https://github.com/makemore/agent_studio |
| `agent/django_agent_runtime/` | https://github.com/makemore/django-agent-runtime |
| `agent/django_agent_studio/` | https://github.com/makemore/django_agent_studio |
| `clients/agent-frontend/` | https://github.com/makemore/agent-frontend |
| `clients/agent-android/` | https://github.com/makemore/agent-android |
| `clients/agent-unity/` | https://github.com/makemore/agent-unity |
| `clients/agent-client/` | https://github.com/makemore/agent-client |
| `clients/agent-ios/` | https://github.com/makemore/agent-ios |

## Read the docs

The canonical guides live in **[`docs/`](docs/)**:

| Guide | Audience |
|---|---|
| [Product Overview](docs/overview/product-overview.md) | CTOs, technical leads |
| [Server Install](docs/setup/server.md) | Backend / DevOps |
| [Client Install](docs/setup/client.md) | Mobile developers |
| [Managed MCP Setup](docs/setup/managed-mcp.md) | Backend developers |
| [chisel Setup](docs/setup/chisel.md) | Tool authors |
| [Package Registry](docs/reference/package-registry.md) | Maintainers |
| [For AI agents](docs/contributing/agents.md) | AI coding agents |
