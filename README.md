# Agent Libraries

Central documentation for the **agent_libraries** monorepo: the
Python/Django backend, mobile + web clients, Studio UI, and the
managed-MCP feature that lets projects hand end-users their own MCP
servers.

## Read the docs

The canonical guides live in the **[`docs/`](docs/)** directory.

| Guide | Audience |
|---|---|
| [Product Overview](docs/overview/product-overview.md) | CTOs, technical leads — what is this product, when to use it, when not to |
| [Server Install](docs/setup/server.md) | Backend / DevOps — stand up the Django runtime + Studio |
| [Client Install](docs/setup/client.md) | Mobile developers — integrate iOS / Android agent clients |
| [Managed MCP Setup](docs/setup/managed-mcp.md) | Backend developers — turn on multi-connection, per-principal MCP |
| [chisel Setup](docs/setup/chisel.md) | Tool authors — install the chisel + django-chisel stack |
| [Package Registry](docs/reference/package-registry.md) | Maintainers — internal PyPI registry, publishing flow |
| [For AI agents](docs/contributing/agents.md) | AI coding agents — how to integrate the libraries in a new app |

## Source layout

```
agent_libraries/
├── docs/                   ← this documentation site (mkdocs source)
├── agent/                  ← backend Python packages
│   ├── agent_runtime_core/
│   ├── django_agent_runtime/
│   └── django_agent_studio/
├── clients/                ← mobile, web, TS, Unity clients
├── chisel/, django_chisel/ ← tool-builder framework
└── parrot/                 ← agent version-control registry
```

## Per-package documentation

Every package has its own `README.md` with package-local content
(install, settings, API reference). The docs site cross-links into
those for the details; this index stays small.

## Building the docs

The docs site uses [mkdocs](https://www.mkdocs.org/) + the
[Material](https://squidfunk.github.io/mkdocs-material/) theme. Build
locally:

```bash
pip install mkdocs mkdocs-material
cd docs
mkdocs serve      # local preview at http://localhost:8000
mkdocs build      # static site in docs/site/
```

The Cloudflare Pages deploy + GitHub Actions workflow will be added in
a follow-up PR.