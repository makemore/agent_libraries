# Postiz on the shared business-tools VM

This is an application fragment, not a standalone deployment. Leave the older
`infrastructure/agentic-social` scaffolding alone. No deployment or image pull
has been performed for this fragment.

## Root integration contract

Render `compose.yaml.tftpl` with Terraform `templatefile` and only the `images`
and `domains` maps. Install the result at
`/opt/business-tools/agentic-social/compose.yaml`. Merge it into the **same Compose
project** as the root file and Mautic. Root defines `edge` as a normal bridge,
not an external network. Only `postiz` joins it; proxy to **`postiz:5000`**.
`postiz-private` and `postiz-temporal-db` are internal bridges. PostgreSQL,
Redis and Temporal have no public ingress or egress; the bundled application
workers have outbound access through `edge` for social APIs, SMTP and startup.

Required image keys (pin reviewed release digests, not floating `latest`):

| Map key | Required image family |
| --- | --- |
| `postiz` | `ghcr.io/gitroomhq/postiz-app`, Temporal-based releases **2.12.0+**; select a currently supported, tested release |
| `postgres` | Official `postgres:17-bookworm` family, `/var/lib/postgresql/data` layout |
| `redis` | Official `redis:7.2-bookworm` family |
| `temporal` | `temporalio/auto-setup:1.28.1` family, **not** server-only/admin-tools/CLI |
| `temporal_postgres` | **Additional required key:** official `postgres:16-bookworm` family for Temporal |

The two PostgreSQL majors follow the upstream Postiz application/Temporal split;
Bookworm is selected explicitly for predictable numeric ownership. Do not swap in
Alpine (different IDs) or PostgreSQL 18 (different volume layout) without adapting
ownership and testing upgrades. These are compatibility families, not claims that
every patch combination is tested. Root must validate image refs and `domains.social`
as a DNS hostname (no scheme/path/quotes/newlines) before rendering.

Use Docker Compose **2.30.0+** for raw env files. Root owns directory creation,
mountpoint/secret checks, image selection, logging limits, backups, IAP and Caddy.
Root systemd runs merged `docker compose up --abort-on-container-exit`; there are
no one-shot services or Docker restart policies. Allow at least 90 seconds per
database for shutdown and a sufficiently larger systemd stop timeout. Healthchecks
gate startup only: unhealthy running processes do not automatically stop Compose.

## Data directories and ownership

Root prepares these exact bind sources on the mounted data disk **before** Compose.
All binds use `create_host_path: false` so missing paths do not silently become
root-owned directories on the boot disk. This does not replace root's mountpoint check.

| Host path | Container path | Initial host UID:GID / mode |
| --- | --- | --- |
| `/srv/business-tools/postiz/config` | `/config` | `0:0`, `0700` |
| `/srv/business-tools/postiz/uploads` | `/uploads` | `0:0`, `0755` (nginx worker must read media) |
| `/srv/business-tools/postiz/postgres` | `/var/lib/postgresql/data` | `999:999`, `0700` |
| `/srv/business-tools/postiz/redis` | `/data` | `999:999`, `0700` |
| `/srv/business-tools/postiz/temporal-postgres` | `/var/lib/postgresql/data` | `999:999`, `0700` |

No forced `user` is set. The published Postiz build workflow uses `Dockerfile.dev`:
it has no `USER`, runs nginx then PM2 as root, and creates a separate `www` nginx
worker user. Forcing UID 1000 would break that startup contract. PostgreSQL and
Redis's Bookworm Dockerfiles explicitly create UID/GID 999. Temporal's server
Dockerfile uses UID/GID 1000, but needs **no host-writable state directory**: workflow
history and visibility live in its dedicated PostgreSQL database. Preserve its
image config directory instead of obscuring it with an empty bind.

Re-verify identities and image-declared volumes against each pinned digest during
the operator's image smoke test. Never recursively chown unrelated or existing
data on a seed rerun. `/config` may contain credentials; protect it and all backups.

## Secret environment contract

Root writes root-owned `0600` files inside `/run/business-tools` (`0700`). Credentials
are generated/provided out of band, **never Terraform variables/state or source**.
Use raw `KEY=value` lines: no surrounding quotes, `export`, inline comments, NUL,
CR or LF in values. Raw format preserves literal dollar signs. Do not pass these
files as Compose's CLI `--env-file`, print `compose config` with real secrets,
dump container environments, or pass passwords as command arguments.

| File under `/run/business-tools` | Required keys | Constraint |
| --- | --- | --- |
| `postiz.env` | `DATABASE_URL`, `JWT_SECRET` | URL scheme `postgresql`, user/database `postiz`, host `postiz-postgres`, port `5432`; password matches `postiz-db.env`. JWT is a unique cryptographically random secret (at least 32 random bytes). |
| `postiz-db.env` | `POSTGRES_PASSWORD` | Unique strong password for the dedicated Postiz DB role. |
| `temporal.env` | `POSTGRES_PWD` | Exactly matches `temporal-db.env`; do not confuse this key with `POSTGRES_PASSWORD`. |
| `temporal-db.env` | `POSTGRES_PASSWORD` | A different strong password for the dedicated Temporal DB role. |

For the DB passwords, use at least 32 random bytes encoded as 64 hex characters
or unpadded base64url. This avoids URI delimiter ambiguity and Temporal's upstream
YAML-template quoting hazards. When using existing Postiz credentials, correctly
percent-encode the password component of `DATABASE_URL`, not the entire URL.
Temporal's password must contain no quotes/backslashes/control characters.
The DB entrypoints use credentials only on **first initialization**: rotating an
env file does not rotate an existing DB role. Coordinate SQL changes with clients.
The dedicated PostgreSQL bootstrap users have powerful DB privileges; do not share
these databases with unrelated applications. No database password enters Postiz
except via `DATABASE_URL`; Postiz never receives Temporal's DB password.

Public URLs, internal endpoints, role and path choices belong in Compose, not
secret files. All other application variables remain unset unless needed:

- For SMTP, deliberately configure `EMAIL_PROVIDER=nodemailer`, `EMAIL_HOST`,
  `EMAIL_PORT`, `EMAIL_SECURE`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME` in an
  operator-reviewed Compose environment change; put `EMAIL_USER`/`EMAIL_PASS`
  in `postiz.env`. Use provider-approved TLS on 465 or 587 and test delivery.
  Alternatively choose Resend and supply `RESEND_API_KEY`. Its presence also
  changes account activation behavior; configure email **before** the first signup.
- Add only the chosen providers' credentials to `postiz.env`, e.g.
  `LINKEDIN_CLIENT_ID`/`LINKEDIN_CLIENT_SECRET`, `X_API_KEY`/`X_API_SECRET`,
  `FACEBOOK_APP_ID`/`FACEBOOK_APP_SECRET`, `YOUTUBE_CLIENT_ID`/`YOUTUBE_CLIENT_SECRET`.
  Complete each provider's developer approval/scopes and exact HTTPS callback
  registration; social OAuth is distinct from OAuth/OIDC **login**.
- No billing keys, demo data, retention, Redis persistence, API-rate-limit or SSRF
  overrides are imposed. The local upload provider is inherited from upstream.
  Keep `NOT_SECURED` and `DISABLE_SSRF_PROTECTION` unset.

## Temporal and background work

Temporal 1.28 supports **SQL visibility**. Its config template chooses SQL when
`ENABLE_ES` is absent, and auto-setup creates/updates both `temporal` and
`temporal_visibility`. Elasticsearch, Temporal UI and a hanging admin-tools shell
are not necessary. Auto-setup finishes its initialization and execs the server;
there is no one-shot container to trip `--abort-on-container-exit`. Its background
namespace creation is included in the health gate to avoid a startup race.
Postiz's image runs frontend, backend and orchestrator; `RUN_CRON=true` enables
bundled scheduled maintenance. No second cron container or host cron is needed.

This is a single-node deployment, not a highly available Temporal installation.
Back up both Temporal databases together with Postiz's DB and uploads. Keep upstream
namespace retention and dynamic configuration defaults; do not copy the development
force-search-attribute-refresh config from an example into production.

## Secure first installation and activation

1. Keep root's public-readiness toggle **false**: Caddy must deny **all** public
   paths on both domains, not just signup/install pages. Confirm from outside IAP
   that `/`, `/api/`, uploads and arbitrary paths cannot reach the application.
2. Use root's IAP-only loopback proxy/tunnel. Keep the canonical HTTPS social
   hostname, forwarded headers and secure cookies; do not change application URLs
   to localhost or weaken cookies to make the tunnel work. Root handles this route.
3. Check PostgreSQL, Redis, Temporal health and namespace `default`, then create
   the first local Postiz account privately. `DISABLE_REGISTRATION=true` is an
   intentional **single-account security policy**, not a readiness switch: upstream
   allows the first account and blocks subsequent registrations. It also disables
   OIDC/OAuth login. Test a second registration is rejected by the API, not merely
   that the signup page is hidden. Verify login/logout and password recovery.
4. Verify DB-backed settings survive a controlled restart, upload/read an image,
   and schedule an operator-approved test post. Confirm the Temporal workflow and
   actual provider delivery, not just HTTP health. Inspect only sanitized logs.
5. Configure social OAuth and SMTP. Public provider callbacks/media fetching can
   remain unavailable while readiness is false; complete those end-to-end checks
   in a controlled activation window only after account/registration checks pass.
6. Only the operator enables root's public readiness after **both apps** are
   securely configured. Recheck unauthenticated registration denial after enabling.
   For multi-user/OIDC login, first approve a separate access-control design; do
   not casually set `DISABLE_REGISTRATION=false` on an exposed domain.

Pin upgrades and take restorable backups before recreating the image: the current
upstream PM2 startup runs Prisma `db push --accept-data-loss`. Test upgrades on a
restored isolated copy, not live data. Reruns must preserve accounts and settings.

## Sources reviewed (2026-09-20) and verification limits

- [Official installation](https://docs.postiz.com/self-host/installation/docker-compose)
  and [canonical Compose](https://github.com/gitroomhq/postiz-docker-compose/blob/main/docker-compose.yaml).
- [Configuration, registration and SMTP](https://docs.postiz.com/self-host/configuration/reference)
  and [provider OAuth guides](https://docs.postiz.com/self-host/providers/overview).
- Postiz source at `7cef69c12fd5ab486f97f70452cfd3dd3708de4b`:
  [published build](https://github.com/gitroomhq/postiz-app/blob/7cef69c12fd5ab486f97f70452cfd3dd3708de4b/.github/workflows/build-containers.yml),
  [Dockerfile](https://github.com/gitroomhq/postiz-app/blob/7cef69c12fd5ab486f97f70452cfd3dd3708de4b/Dockerfile.dev),
  [nginx port/routes](https://github.com/gitroomhq/postiz-app/blob/7cef69c12fd5ab486f97f70452cfd3dd3708de4b/var/docker/nginx.conf),
  [PM2/schema startup](https://github.com/gitroomhq/postiz-app/blob/7cef69c12fd5ab486f97f70452cfd3dd3708de4b/package.json).
- [Temporal 1.28.1 SQL config](https://github.com/temporalio/temporal/blob/v1.28.1/docker/config_template.yaml),
  [auto-setup](https://github.com/temporalio/docker-builds/blob/main/docker/auto-setup.sh),
  [entrypoint](https://github.com/temporalio/docker-builds/blob/main/docker/entrypoint.sh),
  [image identity](https://github.com/temporalio/docker-builds/blob/main/server.Dockerfile).
- [PostgreSQL 17 ownership/layout](https://github.com/docker-library/postgres/blob/master/17/bookworm/Dockerfile),
  [PostgreSQL 16](https://github.com/docker-library/postgres/blob/master/16/bookworm/Dockerfile),
  [Redis ownership/layout](https://github.com/redis/docker-library-redis/blob/master/7.2/debian/Dockerfile).
- [Compose raw env files and bind semantics](https://docs.docker.com/reference/compose-file/services/).

Run `python3 -m unittest discover -s infrastructure/business-tools/agentic-social/tests -v`
from the repository root. Tests are isolated static checks, with an optional
configuration-only Compose check using temporary **empty** env files. No Docker
daemon, image pull, live credentials or running apps are needed. These checks do
not prove image startup, OAuth, SMTP, backups or root's readiness/IAP integration.