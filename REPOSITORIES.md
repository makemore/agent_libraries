# Repository organization

This workspace is a checkout coordinator, not a monorepo. Product and package
history stays in independent repositories.

## Canonical ownership

- **Reusable Python libraries:** `agent/agent_runtime_core`,
  `agent/django_agent_runtime`, `agent/django_agent_studio`,
  `agent/django_agent_sdlc`, `chisel`, and `django_chisel`.
- **Host applications/products:** `agent/agent_studio`, `jimmy`, `warp`, and
  `parrot`.
- **Clients:** `clients/agent-frontend`, `clients/agent-cli`, and the Android,
  iOS, and Unity repositories.
- **Shared workspace material:** root scripts, plans, assets, test harnesses,
  skills, and deliberately vendored code.

`clients/agent-frontend/packages/agent-client` is the canonical TypeScript
client. `clients/agent-client` is a legacy checkout and receives no new work.

## Target layout

Keep current package paths until their deployment and editable-install callers
are migrated. The eventual top-level shape should be:

- `packages/` — reusable Python and JavaScript libraries;
- `products/` — Studio host, Jimmy, Warp, Parrot, Conduit, and ManyHands;
- `clients/` — CLI and platform clients;
- `infrastructure/` — Machine and deployment tooling;
- `vendor/`, `plans/`, `assets/`, and `test-harness/` — shared support.

## Migration order

1. Use `scripts/checkout.sh` as the complete reproducible repository inventory.
2. Retire the legacy standalone `clients/agent-client` checkout after confirming
   no release automation uses it.
3. Lift `conduit`, `machine`, and `manyhands` out of `warp/other_projects`.
4. Move reusable packages under `packages/`, updating checkout, install,
   release, wheel-staging, frontend-copy, and CI paths in the same change.
5. Move product repositories only after package paths are stable.

Do not combine path moves with product behavior changes or repository-history
merges. Each move should preserve its existing remote and be independently
reversible.