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

## Development defaults

Contributors and coding agents must follow [the setup/defaults policy](AGENTS.md):
general demos, seeds and fixtures inherit supported defaults, use normal write
paths, and keep compatibility overrides explicit and locally justified. Persistent
demo setup must have a reproducible, tested routine rather than exist only in
terminal history. Setup permission does not authorize changing product semantics.

## Jimmy (local standalone project)

[`jimmy/`](jimmy/README.md) is an independent local Python 3.12 coding-agent
repository, with a Textual interface, one-shot CLI, and ACP stdio integration.
It uses the patched core directly; it does not install Django or change backend
or mobile workflows. Its isolated environment and lockfile are managed with uv.

From that directory, `uv run --locked jimmy demo --plain` runs a labelled offline
simulation without credentials or workspace writes. Real runs require an explicit
model via `--model` or `JIMMY_MODEL` and an existing OpenAI credential source.
Writes and commands require exact allow-once approval; JSON/noninteractive runs
deny them by default. Approved commands are **not sandboxed**.

Jimmy has no configured remote and is not cloned by `make checkout`. Publication
is blocked on a tested core release, core-commit provenance, licensing decisions,
and manual editor/live-provider acceptance. See its README for current limits.

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

## Live local Studio development

After installing the declared npm dependencies in `clients/agent-frontend` and
`agent/django_agent_studio/frontend`, run **`make watch-frontends`** at this root.
It starts only asset watchers; it does not start workers, consume queued jobs,
migrate data or deploy anything. Stop it with Ctrl-C.

- The widget bundles `clients/agent-frontend/packages/agent-client/src` directly.
  No stale client `dist` build or legacy `clients/agent-client` is used.
- Widget JS, its source map, CSS and markdown plugin are synchronized to
  `agent/django_agent_studio/static/agent-frontend` after successful builds.
  CSS/plugin sources are currently maintained in the widget's `dist` directory.
- Studio's Vite watcher builds into its own Django static directory.
- **Refresh the page after a build.** This is build-and-serve watching, not browser
  HMR. Django development static finders serve these files; no `collectstatic`
  is needed. A production host serves deployed artifacts, not these checkouts.

The root `Procfile` (or `agent/agent_studio/Procfile.dev` from that directory)
also describes reload-enabled web/worker processes, plus both watchers. Use it
with your chosen Procfile supervisor only after installing the host's declared
requirements into the host's own `agent/agent_studio/.venv`, with the library
editable installs (`pip install -e`) pointing at the checkouts here. The root
`.venv` is for library tests only; it does not carry the host's dependencies.
Do not start it alongside another server on port 8001 or an existing worker.
**Starting the worker can execute queued jobs.** Changing these files does not
restart an already-running process launched with `--noreload`.

Python packages do not compile into the web bundle: web and worker processes
must import the editable checkouts and run their reloaders (or be restarted).
Do not infer live Python reload merely from an editable installation.

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
- `make test-jimmy`: run Jimmy's isolated offline pytest suite using its lockfile;
  first run `uv sync --locked` inside the local Jimmy checkout.
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
├── jimmy/                   ← local standalone coding agent (own repo; no remote)
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
