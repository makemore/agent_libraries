# Actual Budget

Actual is a budgeting and account-tracking tool, **not full business accounting**.
This folder is a fragment of the shared business-tools VM, not a standalone stack.

## Verified upstream contract

Reviewed 2026-09-20 against the official **26.9.x** release family (v26.9.0):

- [Docker installation](https://actualbudget.org/docs/install/docker/): official
  `actualbudget/actual-server` (also `ghcr.io/actualbudget/actual`), HTTP port 5006,
  persistent `/data` with `server-files` and `user-files`.
- [Release Dockerfile](https://github.com/actualbudget/actual/blob/v26.9.0/packages/sync-server/docker/ubuntu.Dockerfile):
  Debian/Node runtime; creates `actual` UID/GID **1001:1001**, but does not issue a
  `USER` instruction. This template explicitly selects that non-root identity.
  Do not substitute old images or Alpine variants with different ownership.
- [Upstream Compose](https://github.com/actualbudget/actual/blob/v26.9.0/packages/sync-server/docker-compose.yml):
  `node scripts/health-check.js` is the supported health probe.
- [Server configuration](https://actualbudget.org/docs/config/): password login is
  the default. No invented password environment variable, auth bypass, or forced
  OIDC/header authentication is configured here.

These are source-verified contracts, not a claim that a registry digest has been
smoke-tested. Root must resolve a reviewed patch release to an architecture-correct
digest and repeat the checks below when upgrading.

## Root integration

- Render `compose.yaml.tftpl` with exactly `{images = map, domains = map}`. Required
  image key: `images.actual`, an official image reference ending in `@sha256:...`.
  `domains.budget` belongs to root ingress; Actual needs no domain environment.
- Root Caddy on the root-defined `edge` network proxies the budget domain to
  **`actual:5006`**. This fragment intentionally does not define `edge`.
- No ports are published. No host networking, container names, Docker socket,
  restart policy, independent unit, or detached startup is provided. Root systemd
  runs the entire merged project with `up --abort-on-container-exit`.
- Stage the rendered file at `/opt/business-tools/actual-budget/compose.yaml`.
  There are no additional configuration files or runtime env files for Actual.
- **Keep root `public_enabled = false` until initialization is complete.** This
  must deny ALL public application paths, including setup/API/static paths, not
  merely the login page. Only root's private loopback/IAP entry may reach setup.
  Root must provide browser-trusted HTTPS and preserve Actual's response headers
  (including cross-origin isolation headers needed by the browser database).

## Persistence and bootstrap ownership

| Host bind path | Container path | Initial owner | Mode |
| --- | --- | --- | --- |
| `/srv/business-tools/actual-budget/data` | `/data` | `1001:1001` | `0700` |

Root must mount the data disk and create this directory before Compose starts.
`create_host_path: false` deliberately fails rather than silently making an
incorrectly owned directory on the root disk. On an existing installation,
preserve contents and existing configuration; perform any necessary ownership
migration deliberately with a backup, not by resetting the budget on bootstrap.

Back up the entire data bind (including account/auth state), not only budget
exports. Stop through root's lifecycle owner before a filesystem copy of SQLite
data, or use a separately validated consistent backup procedure.

## Setup and validation

1. On a disposable test VM, render and merge this fragment with root Compose.
   Run `docker compose config --quiet` with root's full file list. Never print a
   resolved merged configuration once other apps' runtime secrets are present.
2. Confirm effective UID/GID is `1001:1001`, `/data` is writable, the image health
   probe passes, and no application host port is published.
3. Verify public requests to `/`, setup, and API paths are denied while the gate
   is closed. Through root's private HTTPS/IAP route, set the initial server
   password using Actual's supported first-run UI. There is **no required secret
   JSON key and no Actual env file** in this deployment.
4. Create a small budget using the UI, add an account/transaction, sign out and
   sign back in. Check unauthenticated access cannot read the budget. Restart the
   merged stack through systemd and confirm the budget and password persist.
5. Only after all apps are initialized may the operator explicitly enable root's
   public gate. Do not bypass ingress or weaken authentication to pass a check.

Docker output uses the `local` driver with `10m` / `3` rotation. A container that
exits stops the merged project; an unhealthy container alone does not trigger
Compose's abort flag. Root monitoring must distinguish these conditions.

Implementation validation: official sources and the template were reviewed;
no image was pulled, no deployment was performed, and no executable validation
was run in the authoring session (no command-execution tool was available).