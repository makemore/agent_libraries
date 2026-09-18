# Repository organization

This root is a thin coordinator, not a product monorepo. The reorganization is
complete: **23 Git submodules**, with no package moves or repository splits
pending. Product and package history stays in independent repositories.

[`.gitmodules`](.gitmodules) is the authoritative inventory of paths and remotes.
The parent repository's gitlinks pin exact child commits; they do **not** certify
that the full suite or every integration has been validated.

## Current layout and ownership

All remotes below are under `github.com/makemore`; use `.gitmodules` for URLs.

| Submodule path | Remote repository | Role |
|---|---|---|
| `packages/python/agent_runtime_core` | `agent-runtime-core` | Core runtime |
| `packages/python/django_agent_runtime` | `django-agent-runtime` | Django runtime |
| `packages/python/django_agent_studio` | `django_agent_studio` | Reusable Studio application |
| `packages/python/django_agent_sdlc` | `django-agent-sdlc` | SDLC tracker |
| `packages/python/chisel` | `chisel` | Tool-builder framework |
| `packages/python/django_chisel` | `django_chisel` | Django tool integration |
| `products/studio` | `agent_studio` | Django host project |
| `products/studio-desktop` | `studio-desktop` | Desktop product; private remote |
| `products/jimmy` | `jimmy` | Standalone coding agent |
| `products/warp` | `warp` | Warp product |
| `products/parrot` | `parrot` | Version-control registry |
| `products/manyhands` | `manyhands` | ManyHands product |
| `products/conduit` | `conduit` | Conduit product |
| `products/shipwright` | `shipwright` | Shipwright product |
| `clients/agent-frontend` | `agent-frontend` | Web/widget and canonical TypeScript client |
| `clients/agent-cli` | `agent-cli` | Studio REST CLI; private remote |
| `clients/agent-android` | `agent-android` | Android client |
| `clients/agent-ios` | `agent-ios` | iOS client |
| `clients/agent-unity` | `agent-unity` | Unity client |
| `infrastructure/machine` | `machine` | Infrastructure |
| `templates/fullstack-cookiecutter` | `fullstack-cookiecutter` | Project template |
| `archive/agent-client` | `agent-client` | Legacy standalone TypeScript client |
| `docs` | `agent-docs` | Canonical documentation |

The active TypeScript client is `clients/agent-frontend/packages/agent-client`
(`@makemore/agent-client`); new work belongs there, not in `archive/agent-client`.
The Studio host (`products/studio`) and reusable Django application
(`packages/python/django_agent_studio`) have distinct ownership.

Root-owned material includes coordinator scripts, plans, assets, shared tests
and fixtures, skills, and **`vendor/ace`**. Vendored ACE is not a submodule;
`docs` is. Product changes belong in their owning child repositories.

## Working with pins safely

1. `make checkout` initializes **only missing** repositories at their pins.
   Existing branches and dirty work are never switched or reset, and existing
   checkouts are not pulled. Private-repository access is required.
2. `make status` reports Git state for the root and all submodules: branch or
   detached state, local changes, and upstream ahead/behind counts.
3. Fresh submodules are normally detached at the pinned commit. Before editing,
   run `git -C <path> switch -c <branch>`.
4. Commit and push the child first. Then stage that child's gitlink in the parent,
   commit and push the parent. Parent pins must reference available child commits.
5. `make clean` only previews ignored files; it deletes nothing.

## Migration notes

- CLI and desktop were extracted with history preserved to the private remotes
  `makemore/agent-cli` and `makemore/studio-desktop`.
- `clients/agent-frontend` and `products/warp` remain pinned to commits on
  `workspace-backup-2026-09-18`. Those branches were deliberately **not merged
  into remote `main`**; do not treat checkout as a request to switch them.
- Existing history was not rewritten. The root `.env` remains untracked and
  local; do not commit credentials or environment files.
- Recreate moved local `.venv` environments and reinstall editable sources using
  the new paths. These local environments are not versioned or safely relocatable.
- Preserve host overrides and product defaults. Follow [AGENTS.md](AGENTS.md)
  and child setup guidance; path migration does not authorize behavior changes.