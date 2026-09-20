# Invoice Ninja

## Verified image family and process model

Reviewed 2026-09-20: Invoice Ninja **v5.13.x Debian** (`invoiceninja/invoiceninja-debian`),
official **Nginx 1.30.x Alpine**, official **MariaDB 11.4.x LTS**, and official
**Redis 7.2.x Bookworm** (the existing Postiz image family, not its Redis instance).
Resolve a reviewed patch of each to a digest at root; these are compatibility
families, not prevalidated digests or permission to use mutable `latest` tags.

The official Debian image at dockerfiles commit
`5bfcf751f70f94e11818e15deefcaba5d283a48f` is **not an all-in-one HTTP image**:

- [Dockerfile](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/Dockerfile)
  uses PHP-FPM and its health probe talks FastCGI to **9000**.
- [Official Compose](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/docker-compose.yml)
  requires separate Nginx, shared public/storage paths, and healthy Redis before
  starting PHP. Its `env_file` loads [debian/.env](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/.env),
  selecting Redis for queue, cache and sessions. The old non-Debian
  `invoiceninja/invoiceninja` image is not interchangeable with this contract.
- [Supervisor programs](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/supervisor/programs.conf)
  include PHP-FPM, **two `queue:work` workers**, and **one `schedule:work` process**.
  Keep the default entrypoint/CMD; do not add duplicate worker or cron services.
- [Initialization](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/scripts/init.sh)
  runs migrations and seeds the initial account in production. It starts as root
  to repair paths, then uses `www-data` for application commands/workers.
- [Nginx configuration](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/nginx/laravel.conf)
  supplies the routing model. Our adaptation executes only `index.php`, not
  arbitrary uploaded PHP, and marks FastCGI requests HTTPS behind root Caddy.

## Compose and root ingress contract

Render `compose.yaml.tftpl` with `{images = map, domains = map}`. Required keys:
`images.invoice_ninja` = Debian image above, `images.invoice_nginx` = official
`nginx` Alpine image, `images.mariadb` = official `mariadb`, and `images.redis` =
official `redis` Bookworm image above, all `@sha256:...`;
`domains.invoices` = canonical HTTPS hostname.

**Root Caddy upstream is `invoice-ninja:80`**, the Nginx service, not FastCGI.
`invoice-app:9000` stays off `edge`. Nginx and PHP share `invoice-private`, an
`internal: true` per-app network; `invoice-db:3306` and `invoice-redis:6379` have
only that network. PHP has an additional normal `invoice-egress` bridge for
SMTP, payment APIs and other outbound access, plus the dedicated `invoice-render`
bridge described below. Redis is dedicated to Invoice Ninja, passwordless
and isolated like Postiz's Redis: no secret env file, edge membership or port mapping.
No app port is published and root alone defines `edge` and ingress.

Root systemd owns the merged `up --abort-on-container-exit` lifecycle. No Docker
restart policies are set. Internal Supervisor restarts are upstream behavior,
including hourly worker recycling; they do not replace root lifecycle ownership.
Allow **at least 1h plus shutdown overhead** in root's systemd stop timeout: the
image's worker `stopwaitsecs` is 3600. Health probes do not detect every worker
failure and an unhealthy status does not itself trigger Compose's abort flag.

Keep root `public_enabled = false` so external requests to ALL application paths
stay blocked until all five apps pass private setup/activation checks. Use root's
private loopback/IAP route with the canonical HTTPS origin for operator login;
do not temporarily disable `REQUIRE_HTTPS` or add host port mappings. Root preserves
the canonical Host and sanitizes forwarded headers. Client IP rate limits here
see Caddy's address: wildcard trusted proxies are intentionally not enabled.

### Private canonical HTTPS assets for local PDFs

The PHP/browser asset route is implemented separately from the laptop IAP tunnel:

- `invoice-render` is an internal `172.30.252.0/29` network with **only Caddy and
  `invoice-app`**. Caddy is `172.30.252.2`, with the network alias
  `domains.invoices` (default `invoices.makemoredigital.com`); PHP is `172.30.252.3`.
  Nginx, databases and other application containers do not join this bridge.
- `APP_URL` remains the canonical HTTPS invoices origin. Its hostname resolves to
  Caddy on this bridge, letting the local renderer fetch assets on **443**, not
  8443 or HTTP. The real hostname, TLS SNI and certificate validation are retained;
  this is not a hosts/URL rewrite to localhost or a TLS-verification bypass.
- **Only while the public gate is closed**, Caddy's invoices host has a
  `remote_ip 172.30.252.3` exception that proxies to `invoice-ninja:80`. It matches
  the transport peer, not `client_ip`, `X-Forwarded-For` or other forwarded headers.
  All other requests to that host on 443 still get 503; the exception grants no
  access to any other origin. It is not a global bypass or an asset-path allowlist.

Check this reserved subnet and Mautic's `172.30.251.0/29` for host/Docker/VPC/VPN
route collisions **before deployment**. Do not renumber one contract alone:
Compose networks/addresses/aliases, Caddy's peer matcher, the Mautic seed and tests
must stay coordinated, with manual review of any existing persisted seed.

This implements routing, **not a complete PDF smoke test or proof of valid TLS**.
Public DNS and Caddy/ACME issuance are still required for the canonical hostname;
the private Docker alias does not supply a certificate. With selected images,
verify valid TLS and full local PDFs including logos/fonts while the public gate
remains closed. Do not open setup, disable certificate verification or use a
hosted renderer to get a passing result.

## Runtime secret mapping (not Terraform inputs)

Root fetches out-of-band runtime JSON and writes these allowlisted env files as
`root:root`, `0600`, under a `0700` `/run/business-tools`. **Compose >= 2.30** is
required for `env_file.format: raw`. Serialize one `KEY=value` per line, without
shell quotes or interpolation; reject CR, LF and NUL. Never log values, dump the
resolved Compose configuration, or include credentials in command arguments.

| Runtime JSON key | Destination | Requirement |
| --- | --- | --- |
| `APP_KEY` | `invoice-ninja.env`: `APP_KEY` | `base64:` followed by canonical base64 of **32 random bytes** (44 base64 characters); generate once out of band and preserve |
| `DB_PASSWORD` | `invoice-ninja.env`: `DB_PASSWORD`; `invoice-db.env`: `MARIADB_PASSWORD` | Same strong nonempty application DB password in both files |
| `MARIADB_ROOT_PASSWORD` | `invoice-db.env`: `MARIADB_ROOT_PASSWORD` | Separate strong nonempty root DB password; never send to PHP |
| `IN_USER_EMAIL` | `invoice-ninja.env`: `IN_USER_EMAIL` | Initial administrator email |
| `IN_PASSWORD` | `invoice-ninja.env`: `IN_PASSWORD` | Strong initial administrator password, never an upstream example |

These names define this folder's JSON-to-env contract; do not pass the whole JSON
to every container. No DB user/database secret is required: both are fixed to
`invoiceninja`. Validate APP_KEY decoding before startup; [app config](https://github.com/invoiceninja/invoiceninja/blob/v5-stable/config/app.php)
uses AES-256-CBC. The current entrypoint requires initial account credentials for
an empty account table, even though the [README](https://github.com/invoiceninja/dockerfiles/tree/debian#initial-account-setup)
mentions legacy fallback credentials. Reject missing/blank initial values.
After successful setup these two bootstrap account env keys can be removed on an
explicit operator request; never regenerate APP_KEY on restart or reset users.
DB password rotation requires changing the DB user as well as the runtime secret;
MariaDB initialization variables do not rotate an existing database's password.

### Optional SMTP and upstream runtime defaults

SMTP is not a prerequisite for initial login, but invoice delivery and password
reset email need configured transport. Root may optionally allowlist these JSON
keys unchanged into `invoice-ninja.env`: `MAIL_MAILER` (`smtp`), `MAIL_HOST`,
`MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_ENCRYPTION`, `MAIL_FROM_ADDRESS`,
`MAIL_FROM_NAME`, and `MAIL_EHLO_DOMAIN`. See [official mail config](https://github.com/invoiceninja/invoiceninja/blob/v5-stable/config/mail.php).
Keep TLS peer verification enabled. Do not use a log mailer as proof of delivery.

Use the **official Debian deployment defaults**, not the database-queue/file-store
alternative. The pinned `debian/.env` above sets `QUEUE_CONNECTION`, `CACHE_DRIVER`
and `SESSION_DRIVER` to `redis`. These three settings must be supplied here because
our runtime env file contains allowlisted secrets/SMTP settings, not upstream's
Compose env file. Only the Redis hostname changes to `REDIS_HOST=invoice-redis`;
port, client, queue name, Redis databases and password behavior are inherited.
Do not invent a Redis URL or copy upstream's example credentials/debug/proxy settings.

**Deployment defaults are not packaged application defaults.** The Dockerfile
extracts the release archive and never copies `debian/.env` into the image.
The [v5.13.0 release workflow](https://github.com/invoiceninja/invoiceninja/blob/v5.13.0/.github/workflows/react_release.yml)
removes `.env` before packaging; its [.env.example](https://github.com/invoiceninja/invoiceninja/blob/v5.13.0/.env.example)
uses sync queue/file cache/file sessions, and [queue.php](https://github.com/invoiceninja/invoiceninja/blob/v5.13.0/config/queue.php)
also falls back to `sync`. Merely omitting all three drivers would not reproduce
the official Debian Compose deployment. Verify the effective drivers against
the final image digest rather than assuming an embedded Redis `.env` exists.

Keep the Redis image's default command and persistence policy; `/data` is bound
persistently without imposing AOF, eviction or retention overrides. This is not
a zero-loss durability guarantee. If replacing an existing database-queue/file-store
deployment, require an operator-approved transition that accounts for pending jobs
and sessions; do not silently migrate data or reset deliberate host overrides.

`PDF_GENERATOR=snappdf` selects the bundled local browser instead of sending
invoice contents to a hosted renderer. The [application config](https://github.com/invoiceninja/invoiceninja/blob/v5-stable/config/ninja.php)
defines the setting and [upstream image guidance](https://github.com/invoiceninja/dockerfiles/issues/743)
confirms no browser download is needed: the entrypoint selects Chrome/Chromium
by architecture. Assets referencing `APP_URL` use the implemented
[private canonical HTTPS route](#private-canonical-https-assets-for-local-pdfs).
Full rendering with logos/fonts and certificate validation still needs a live
selected-image smoke test while public ingress remains closed.

## Bind directories and root bootstrap

| Host path under `/srv/business-tools/invoice-ninja/` | Container path | Initial ownership/mode |
| --- | --- | --- |
| `public` | `/var/www/html/public` | `33:33`, `0755` |
| `storage` | `/var/www/html/storage` | `33:33`, `0755` |
| `mysql` | `/var/lib/mysql` | **`0:0`, `0700` on first creation**; official entrypoint changes owner to image `mysql:mysql` before dropping privileges |
| `redis` | `/data` | `999:999`, `0700` for the official Redis Bookworm family |

Root's `runtime/prepare-disk.py` already includes
`/srv/business-tools/invoice-ninja/redis` in its directory inventory, creating a
missing directory with ownership `999:999` and mode `0700` before startup. The [Redis 7.2
Bookworm Dockerfile](https://github.com/redis/docker-library-redis/blob/master/7.2/debian/Dockerfile)
explicitly creates that UID/GID; recheck the selected digest. Reuse `images.redis`,
not the Postiz data directory or Redis instance. Do not reset existing ownership.

UID/GID 33 is Debian's [www-data account](https://salsa.debian.org/debian/base-passwd/-/blob/master/passwd.master).
Nginx's [Alpine image](https://github.com/nginx/docker-nginx/blob/master/stable/alpine-slim/Dockerfile)
uses worker `101:101` and only reads the public/storage mounts. The Invoice Ninja
entrypoint enforces files `0644` / dirs `0755` there; keep host parent directories
root-only, and protect backups because the storage contains private data.
It also **replaces generated `public` contents on image startup**; do not put
operator-managed files there. The image cannot use a read-only root filesystem.

MariaDB's [Dockerfile](https://github.com/MariaDB/mariadb-docker/blob/master/11.4/Dockerfile)
allocates `mysql` dynamically with `useradd -r`, not a documented fixed numeric ID.
Its [entrypoint](https://github.com/MariaDB/mariadb-docker/blob/master/docker-entrypoint.sh)
repairs data ownership and uses `gosu mysql`. Do not guess `999:999` or force a
Compose user. Root must create a fresh bind as above and **not reset existing
ownership on each bootstrap**. Record `id mysql` from the chosen image during
validation; retain that numeric ownership in backups/restores.

Stage `nginx.conf` at `/opt/business-tools/invoice-ninja/nginx.conf`, root-owned
`0644`, and the rendered Compose alongside it. Mount creation is fail-closed.
Before upgrades back up the DB consistently plus storage/public, Redis state and
APP_KEY; account for queued jobs when coordinating backups and test restoration
privately. Automatic migrations can make image-only rollback
unsafe. Never treat a crash-consistent disk snapshot as a tested DB restore.

## Validation checklist (isolated VM; not production writes)

1. Render and merge with root. Run `docker compose config --quiet` with the full
   root file list and verify no published ports or DB/Redis membership on `edge`.
   Confirm root prepared the Redis bind with the ownership above; Compose must
   not auto-create it.
   Verify the two-peer renderer network, canonical alias and exact addresses;
   resolve reserved-subnet collisions before startup without changing one side
   of the routing contract alone.
2. With root-owned startup, confirm MariaDB's `healthcheck.sh --connect
   --innodb_initialized`, Redis's `redis-cli ping` (`PONG`), and the inherited PHP
   FastCGI `/health` probe pass. Confirm PHP waits for healthy DB and Redis.
   In the merged project run `docker compose exec -T invoice-ninja nginx -t`.
3. Confirm PHP-FPM, two workers and scheduler are running from process metadata
   (report names/counts only, not full command lines or environments). Inspect only
   effective queue/cache/session driver names and Redis hostname: expect `redis`
   for all three and `invoice-redis`, never dump config/env or job/session payloads.
   The [upstream Supervisor config](https://github.com/invoiceninja/dockerfiles/blob/5bfcf751f70f94e11818e15deefcaba5d283a48f/debian/supervisor/supervisord.conf)
   deliberately disables its control socket, so `supervisorctl status` is not a
   supported check. A successful HTTP response alone is insufficient.
4. Confirm external setup/API/arbitrary requests are denied, including requests
   with forged forwarded headers. From PHP verify canonical HTTPS assets on 443
   with real certificate validation; the renderer exception must not open any of
   the other four origins. Log in via private HTTPS using the supplied initial
   account. Test a draft invoice, local PDFs including logos/fonts, client portal
   permissions, an approved queued email to an operator-controlled test mailbox,
   and a scheduled action. Check Redis jobs drain and failures do not grow. Send a
   harmless test URL marker and confirm Nginx emits no access-log record. Report
   states/counts/exit codes, not financial content, credentials or response bodies.
5. Restart via root systemd; verify DB records, attachments, credentials and
   Redis persistence/session behavior and scheduler still work. Complete an
   isolated restore drill with matching data/config/keys. Only after all five
   apps pass the root activation gates may the operator enable public ingress.

Docker output is bounded at `10m` / `3` files. `LOG_CHANNEL=stderr` sends ordinary
Laravel logs there. Nginx's server-level `access_log off` deliberately disables
inherited combined access logs: invoice/payment/reset URLs can carry tokens in
paths and queries. Scope: this Nginx server; impact: no per-request access trail;
verify with the harmless-marker check above. Error/application logs may still
contain sensitive request context; keep them private and do not enable debug logs.
Named Invoice Ninja channels can still write storage log files. Root bootstrap
validates the supplied `logrotate.conf` with debug output suppressed and installs
it at `/etc/logrotate.d/business-tools-invoice` (source stays
`/opt/business-tools/invoice-ninja/logrotate.conf`). Daily rotation bounds retained
generations, not growth between runs; monitor disk use. Root execution is necessary
to traverse the root-only host parents; `copytruncate` can lose a small number of
lines during rotation. Do not enable arbitrary debug output or publish application
logs. The host rotation configuration must remain root-owned.

Root's `tests/test_stack.py` covers the two-peer internal network, fixed addresses,
canonical alias and closed/open gate structure. Its optional already-local Caddy
adapter check verifies the exact transport-peer matcher survives adaptation; it
does not fetch an image, exercise PHP/Chromium or establish TLS. Run it using the
[root validation instructions](../README.md#local-validation-versus-launch-evidence)
and record passes and skips. These integration fixes have **not yet been
runtime-tested**. Selected-image queue/SMTP/full-PDF and live TLS/IAP smoke tests,
plus an isolated restore drill, remain launch gates, not claimed passing tests.