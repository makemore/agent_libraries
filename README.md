# Agent Libraries

A **meta-repo** that ties together the agent platform's independent
repositories: Python/Django backend, mobile + web clients, Studio UI,
the chisel tool-builder framework, the parrot registry, and the docs
site.

## Package ownership

The active TypeScript client lives in
[`clients/agent-frontend/packages/agent-client`](clients/agent-frontend/packages/agent-client/)
and is published as `@makemore/agent-client`. The standalone
`clients/agent-client` checkout is legacy; new checkouts do not clone it.
Existing legacy checkouts are left untouched.

`agent/agent_studio` is the Django host project;
`agent/django_agent_studio` is the reusable Studio application.
These are different roles, not interchangeable package locations.

## One-shot checkout

```bash
git clone https://github.com/makemore/agent_libraries.git
cd agent_libraries
make checkout    # clones current sub-repos into the right relative paths
make install     # editable-install the Python packages
```

`make checkout` is idempotent — re-running it skips any sub-repo that's
already present. Access to private repositories is required. It does not
pull existing checkouts or guarantee a tested combination of versions.

## Tests

Shared fixtures live in `test-harness/fixtures/{sse,ephemeral}`. Client tests
prefer this location in a workspace and retain package-local fixture fallbacks
for standalone checkouts.

Install the already-declared test dependencies in an isolated environment:

```bash
python3 -m venv test-harness/stub-server/.venv
test-harness/stub-server/.venv/bin/python -m pip install -r test-harness/stub-server/requirements.txt
cd clients/agent-frontend && npm ci && cd ../..
make test PYTHON=test-harness/stub-server/.venv/bin/python
```

Additional targets:

- `make test-ios`: headless Swift client tests on macOS, using a temporary
  package without downloading WhisperKit. This is not an iOS simulator/UI test.
- `make test-android`: both Android JVM test modules; requires a configured
  JDK/Android SDK and cached Gradle dependencies (`--offline`).
- `make test-harness` / `make test-web`: run either portion independently.
- `make clean`: preview ignored files in exact repository directories;
  **does not delete anything**.

The meta-repo CI checks the stub server. The frontend repository separately
checks its TypeScript client against these public fixtures, and the docs
repository builds with MkDocs strict mode. These are focused checks, not a
full backend/device compatibility matrix.

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
| `clients/agent-client/` (legacy; not cloned by default) | https://github.com/makemore/agent-client |
| `clients/agent-ios/` | https://github.com/makemore/agent-ios |

## Read the docs

The canonical guides live in **[`docs/`](docs/)**:

| Guide | Audience |
|---|---|
| [Product Overview](docs/docs/overview/product-overview.md) | CTOs, technical leads |
| [Server Install](docs/docs/setup/server.md) | Backend / DevOps |
| [Client Install](docs/docs/setup/client.md) | Mobile developers |
| [Managed MCP Setup](docs/docs/setup/managed-mcp.md) | Backend developers |
| [chisel Setup](docs/docs/setup/chisel.md) | Tool authors |
| [Package Registry](docs/docs/reference/package-registry.md) | Maintainers |
| [For AI agents](docs/docs/contributing/agents.md) | AI coding agents |
