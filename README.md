# Agent Libraries

A thin coordinator for **23 Git submodules**: reusable Python packages,
products, clients, infrastructure, templates, archived code and documentation.
Each child repository owns its source and history; the root owns workspace
tooling, shared collateral and the infrastructure sources described below.

[`.gitmodules`](.gitmodules) is the authoritative path/remote inventory; Git
gitlinks pin exact child commits. **Pinned does not mean the full suite or
cross-product compatibility has been validated.** See
[REPOSITORIES.md](REPOSITORIES.md) for the complete layout and ownership.

The root-owned [AI gateway deployment](infrastructure/ai-gateway/README.md)
contains OpenTofu configuration for Bifrost at `llms.makemoredigital.com`.
The separate [business-tools deployment](infrastructure/business-tools/README.md)
groups Postiz, Mautic, Actual Budget, Invoice Ninja and Grafana/Prometheus on one
shared VM; it does not combine the AI gateway or Raise CRM with that host.
The copied [Raise CRM source](infrastructure/raise-crm/backend/readme.md) is also
root-owned but separately operated. [Pipeboard](infrastructure/pipeboard/README.md)
documents the selected hosted ads-management integration.

## Get started

1. Clone `https://github.com/makemore/agent_libraries.git` and enter the checkout.
2. Run `make checkout` to initialize missing repositories at their pinned commits.
   Access to private remotes is required.
3. Create and activate a local environment: `python3 -m venv .venv`, then
   `source .venv/bin/activate`. Run `make install` for the workspace's supported
   editable installs; consult each child README for additional setup.
4. Run `make status` to see Git state across the root and all submodules:
   branch/detached state, local changes, and upstream ahead/behind counts.

`make checkout` never switches or resets existing branches or dirty worktrees,
and does not pull existing repositories. A fresh submodule checkout is normally
**detached at its pin**; that is expected, not an error.

Before changing a child, create a branch with `git -C <path> switch -c <branch>`.
Commit and push the **child first**, then stage its gitlink in the root, commit
and push the parent. A parent gitlink does not publish child commits. Review
`make status` before either commit; `make clean` is **preview-only** and deletes
nothing.

After migration, **recreate moved local `.venv` environments and reinstall
editable sources at their new paths**; virtual environments are not relocatable
or versioned. The root `.env` remains untracked and local. Existing history has
not been rewritten. No package moves or repository splits remain pending.

## Package ownership

- Reusable Python packages live in `packages/python/`; product hosts live in
  `products/`. In particular, [`products/studio`](products/studio/) is the Django
  host, while [`packages/python/django_agent_studio`](packages/python/django_agent_studio/)
  is the reusable application. They are not interchangeable.
- The active TypeScript client is
  [`clients/agent-frontend/packages/agent-client`](clients/agent-frontend/packages/agent-client/),
  published as `@makemore/agent-client`. [`archive/agent-client`](archive/agent-client/)
  is legacy and receives no new work.
- [`clients/agent-cli`](clients/agent-cli/README.md) and
  [`products/studio-desktop`](products/studio-desktop/) are independent,
  history-preserving extractions with private remotes `makemore/agent-cli` and
  `makemore/studio-desktop`; neither is root-owned product code.
- `clients/agent-frontend` and `products/warp` remain pinned to commits on
  `workspace-backup-2026-09-18`, deliberately not merged into remote `main`.
- [`docs`](docs/) is its own repository. `vendor/ace` remains root-owned.

The CLI supplies **`studio`**, a
standalone, dependency-free Python REST client for runtime and Studio metadata
reads. It uses explicit connection profiles and host-approved credentials, not
Django settings or database access. Not yet published or part
of `make install`. SDLC remote authentication and write commands remain future work.

[`packages/python/django_agent_sdlc`](packages/python/django_agent_sdlc/README.md) is the independent
Django-only issue tracker: private scopes, work items, notes/checklists,
and session UI/API. M0 is isolated-tested; approval/runtime workflows and provider
sync remain planned. It is initialized by `make checkout`, but is not yet published or
included in `make install`; no live host is enabled. See its README for installation.

## Development defaults

Contributors and coding agents must follow [the setup/defaults policy](AGENTS.md):
general demos, seeds and fixtures inherit supported defaults, use normal write
paths, and keep compatibility overrides explicit and locally justified. Persistent
demo setup must have a reproducible, tested routine rather than exist only in
terminal history. Setup permission does not authorize changing product semantics.

## Jimmy

[`products/jimmy`](products/jimmy/README.md) is an independent Python 3.12 coding-agent
repository, with a Textual interface, one-shot CLI, and ACP stdio integration.
It uses the patched core directly; it does not install Django or change backend
or mobile workflows. Its isolated environment and lockfile are managed with uv.

From that directory, `uv run --locked jimmy demo --plain` runs a labelled offline
simulation without credentials or workspace writes. Real runs require an explicit
model via `--model` or `JIMMY_MODEL` and an existing OpenAI credential source.
Local tool actions are auto-approved by default (`approvals.mode = "all"`),
including JSON/noninteractive runs; user settings can explicitly select `ask` or
`edits`. Plan mode and workspace restrictions still apply. MCP, managed-profile
changes and undo/rewind retain separate approval; unattended requests needing
approval are denied. Commands are **not sandboxed**.

Jimmy's remote is `makemore/jimmy`; `make checkout` initializes its pinned commit.
Publication remains
blocked on a tested core release, core-commit provenance, licensing decisions, and
manual editor/live-provider acceptance. See its README for current limits.

## Live local Studio development

After installing the declared npm dependencies in `clients/agent-frontend` and
`packages/python/django_agent_studio/frontend`, run **`make watch-frontends`** at this root.
It starts only asset watchers; it does not start workers, consume queued jobs,
migrate data or deploy anything. Stop it with Ctrl-C.

- The widget bundles `clients/agent-frontend/packages/agent-client/src` directly.
  No stale client `dist` build or legacy `archive/agent-client` is used.
- Widget JS, its source map, CSS and markdown plugin are synchronized to
  `packages/python/django_agent_studio/static/agent-frontend` after successful builds.
  CSS/plugin sources are currently maintained in the widget's `dist` directory.
- Studio's Vite watcher builds into its own Django static directory.
- **Refresh the page after a build.** This is build-and-serve watching, not browser
  HMR. Django development static finders serve these files; no `collectstatic`
  is needed. A production host serves deployed artifacts, not these checkouts.

The root `Procfile` (or `products/studio/Procfile.dev` from that directory)
also describes reload-enabled web/worker processes, plus both watchers. Use it
with your chosen Procfile supervisor only after installing the host's declared
requirements into the host's own `products/studio/.venv`, with the library
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

For the offline harness and web checks, install the declared dependencies with
`.venv/bin/python -m pip install -r test-harness/stub-server/requirements.txt`
and `npm ci` inside `clients/agent-frontend`, then run
`make test PYTHON=.venv/bin/python` from the root. Django/pytest checks use the
root `.venv/bin/python` and isolated test settings, **never the host database**.

Additional targets:

- `make test-ios`: headless Swift client tests on macOS, using a temporary
  package without downloading WhisperKit. This is not an iOS simulator/UI test.
- `make test-android`: both Android JVM test modules; requires a configured
  JDK/Android SDK and cached Gradle dependencies (`--offline`).
- `make test-harness` / `make test-web`: run either portion independently.
- `make test-jimmy`: run Jimmy's isolated offline pytest suite using its lockfile;
  first run `uv sync --locked` inside `products/jimmy`.

The meta-repo CI checks the stub server. The frontend repository separately
checks its TypeScript client against these public fixtures, and the docs
repository builds with MkDocs strict mode. These are focused checks, not a
full backend/device compatibility matrix.

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
