# Mautic on the shared business-tools VM

Application-only fragment for the shared GCE VM, separate from Postiz. No VM,
firewall, Caddy, credential generation or deployment is implemented here.

## Root integration and supported image families

Render `compose.yaml.tftpl` with Terraform `templatefile` using `images` and
`domains` maps. Install at `/opt/business-tools/mautic/compose.yaml` and merge with
root and the other four application groups in one Compose project. Root defines
a normal, **non-external** `edge` bridge. Caddy proxies to **`mautic-web:80`**:
that alias exists only on `mautic-proxy`, an internal `172.30.251.0/29` bridge with
exactly two peers, Caddy at `172.30.251.2` and Mautic web at `172.30.251.3`. Using
the alias keeps proxy ingress on that bridge rather than the shared edge.
No application ports are published. The DB only joins `mautic-private`
(`internal: true`). Web joins that network, `mautic-proxy` and `edge`, retaining
`edge` for egress, not broad proxy trust. Cron/workers join the private network
plus `mautic-egress` (ordinary bridge) for outbound mail, integrations and webhooks
without joining the shared edge or proxy bridge.

Before deployment, check `172.30.251.0/29` and the Invoice renderer's
`172.30.252.0/29` against host/Docker/VPC/VPN routes. Do not renumber one contract
alone: root/app Compose addresses, the seed below, Caddy routing/matchers and tests
must remain coordinated. Existing settings are not automatically migrated.

| Map key | Required family |
| --- | --- |
| `images.mautic` | Official `mautic/mautic:6-apache` family, PHP 8.3/Bookworm; same reviewed release digest for all three roles |
| `images.mariadb` | Official `mariadb:11.4` LTS family with `healthcheck.sh` |
| `domains.marketing` | Canonical DNS hostname, no scheme/path/quotes/newlines |

The source contract is Mautic's modern **5+** multi-role image, targeted here to
the documented 6.x Apache family. Do not use old Mautic 4 images or FPM images
with this port/path contract. Current upstream Docker documentation warns against
the example FPM/nginx setup. Pin maintained patch digests after release/security
review; no digest or full runtime compatibility is asserted by this fragment.

Docker Compose **2.30.0+** is required for raw env files. Root systemd owns merged
`docker compose up --abort-on-container-exit`, restarts and disk/secret gating.
There are no one-shot containers, container names, Docker restart policies or
Docker socket mounts. Root must budget shutdown time beyond the 90-second database
grace period, configure bounded logs, and monitor health beyond startup dependencies.

## Bind directories and identity evidence

Root must prepare these on the mounted data disk before starting Compose. Missing
bind sources fail instead of being auto-created. All three Mautic roles share
the **same four** application directories.

| Host path | Container path | Initial UID:GID / mode |
| --- | --- | --- |
| `/srv/business-tools/mautic/config` | `/var/www/html/config` | `33:33`, `0700` |
| `/srv/business-tools/mautic/logs` | `/var/www/html/var/logs` | `33:33`, `0750` |
| `/srv/business-tools/mautic/media/files` | `/var/www/html/docroot/media/files` | `33:33`, `0750` |
| `/srv/business-tools/mautic/media/images` | `/var/www/html/docroot/media/images` | `33:33`, `0750` |
| `/srv/business-tools/mautic/mariadb` | `/var/lib/mysql` | `0:0`, `0700` initially; upstream root entrypoint assigns `mysql:mysql` before dropping privileges |

Mautic's Dockerfile declares **four separate VOLUMEs** at precisely these targets.
Mounting only their parent `media` could leave anonymous child volumes. Do not
mount an empty directory over the entire application codebase. Its entrypoint
checks directories and assigns `www-data:www-data`; Debian Bookworm's base-passwd
defines that account as **33:33**. Keep image root initialization available for
Apache, cron and supervisor; do not force all roles to UID 33.

MariaDB's official Dockerfile creates a system `mysql` account **without an explicit
numeric ID**. Do not claim a verified fixed `999:999` from that source. Preparing
an empty `0:0` directory is supported: `docker_create_db_directories` chowns it to
the image's `mysql` account before `gosu mysql`. If root runtime requires final
numeric ownership, inspect the selected digest's `id mysql` during approved image
validation and record that result; do not pull an image merely for these tests.
On existing data preserve the verified owner; never reset recursively on reruns.

Config contains database, application and possibly SMTP secrets. Protect config,
logs, databases and backups. Persisted application settings are operator-owned;
do not replace `local.php`, reset installation, or seed fake contacts on a rerun.
Plugin/theme customization belongs in a reviewed derived image, not ephemeral
container changes. Back up config/media with a consistent DB backup before updates.

### Disposable application cache (approved 2026-09-20)

The reviewed Mautic **6.0.9** image ships build-time compiled containers under
`/var/www/html/var/cache/prod/Container*`, including session `cookie_secure=false`.
Recreating from that image alone can retain the wrong compiled behavior even with
the correct canonical HTTPS `site_url` already stored in `local.php`: this is
**image-build cache pollution**, not permission to reset persisted settings.
Upstream `app/config/config.php` derives session-cookie security from `site_url`
outside the installer. The local `cookie_secure` parameter controls tracking
cookies, not this session setting; do **not** add a cookie override as a fix.

The shared `x-mautic-app` anchor now mounts `/var/www/html/var/cache` as a separate
container-private tmpfs for **web, cron and optional worker**:
`rw,nosuid,nodev,noexec,size=256m,uid=33,gid=33,mode=0770`. Each role rebuilds its own
cache from supported configuration instead of reusing the image's compiled cache.
This cache is **not shared or retained business data** and is not backed up. The
four shared application binds, database, `local.php`, narrow proxy trust, official
entrypoints, worker profile and healthchecks are unchanged. No startup command
deletes cache or rewrites operator configuration.

Each tmpfs is bounded to 256 MiB; memory used is **charged to that container's
existing memory limit**, not extra reserved capacity. Account for compilation
memory/startup cost and monitor limits; enabling workers adds another independent
cache. Reassess and remove this workaround when an upstream image fixes the
build-time cache pollution and the regression check passes without the mount.
Removal is a reviewed image/configuration change, not a data migration.

The named **isolated-mautic-cache** exception in
[`check-mautic-cache.py`](../scripts/check-mautic-cache.py) reuses the smoke harness:
real templates, pinned already-local linux/amd64 images, fresh project volumes,
private synthetic env files, internal-only networks, no published ports, no pulls
and no cloud/production access. Preview is the default and does not contact the
Docker daemon. Preview counts **4 services / 3 images**: web, cron, MariaDB and
`cache-proxy`; the one-shot volume-initialization helper is excluded from service
counts and reuses the same Caddy image. It requires OpenTofu and Compose 2.30+:

<augment_code_snippet mode="EXCERPT">
````sh
.venv/bin/python -B infrastructure/business-tools/scripts/check-mautic-cache.py
.venv/bin/python -B -m unittest discover -s infrastructure/business-tools/tests -p test_mautic_cache.py -v
````
</augment_code_snippet>

An approved local `--run` installs only a synthetic administrator through the
supported CLI, using `https://marketing.makemoredigital.com` without reaching that
host. Its fixed fixture password is deliberately non-secret and must **never** be
used for production. The script then force-recreates **only fixture web + cron**
twice (a test-only lifecycle exception, not a production maintenance command).
The in-web-container cookie probe requests `http://172.30.251.2:8080/s/login` with
the canonical Host header through the real fixture proxy; it requires HTTP 200,
Secure cookies and an HttpOnly session cookie. Checks emit booleans only for
cookies, exact stored URL, one installed administrator/admin role, unchanged proxy
seed, tmpfs and discarded cache markers, reading shared state from both roles.
Headers, credentials and CLI diagnostics are captured, never printed.

Named **HTTP-transport-only `cache-proxy`** exception: direct HTTP to web returns
the mandatory-HTTPS 301 before issuing cookies, even with the canonical Host.
This cookie regression alone therefore simulates TLS termination using the
already-local pinned Caddy at the already trusted `172.30.251.2`, with web still
at `.3` on the existing internal `mautic-proxy` bridge. A fixed, public, ephemeral
read-only Caddyfile listens on HTTP `:8080`, disables automatic HTTPS and the admin
API, and proxies only to `mautic-web:80`, setting upstream Host to
`marketing.makemoredigital.com` and `X-Forwarded-Proto` to `https`. No caller can
choose another target; model assertions guard the URL, subnet, address and alias.
This is **not a TLS connection or TLS-verification bypass**, and proves no live
TLS/IAP behavior. Production proxy trust, cookie settings, canonical URLs,
healthchecks, resource limits and native entrypoints are not changed.

The fixture proxy joins only that bridge, publishes no ports and has no egress
or real-data mounts. Its root is read-only, capabilities are dropped except
`NET_BIND_SERVICE` (required by the pinned binary's file capability even on 8080),
and it retains the production Caddy 256 MiB / 128 PID limits and
`no-new-privileges`. Only `/config`, `/data` and `/tmp` are writable, each a bounded
16 MiB tmpfs; the sole host bind is the invocation's public Caddyfile. The proxy
starts with the fixture group only after all three images pass the existing
local-only preflight. `test_http_transport_only_proxy_preserves_defaults_and_isolation` in
`tests/test_mautic_cache.py`, its rejection/orchestration cases and the opt-in
selected-image COOKIE probe verify the exception. Remove it when the regression
uses an approved isolated, certificate-valid TLS fixture; never promote this
HTTP-only configuration to production.

Named CLI compatibility case: `docker exec` bypasses the image entrypoint's
export of `MAUTIC_DB_PORT`. The upstream first-run `local.php` otherwise calls
`file_get_contents` with an empty filename. The fixture CLI wrapper alone replays
the entrypoint's `3306` fallback, preserving an existing port; no Compose or
persisted setting changes. Remove this shim when upstream handles the missing
port. It also runs the supported metadata-storage initialization before the
fresh install, matching the image's test-data install sequence without loading
contacts. Both steps are covered by `tests/test_mautic_cache.py` and the opt-in
selected-image regression; never run them as a repair of an existing database.

Cleanup irreversibly removes only that invocation's exact fresh project/volumes;
cleanup failure fails the check. `tests/test_mautic_cache.py` covers real-template
inheritance/preservation and mocked execution/cleanup failures. Run it and the
existing Mautic/stack suites after changes, recording skips. This regression
check is not proof of live TLS/IAP, early-installer proxy detection, authentication,
delivery or restore. On 2026-09-21 the selected-image fixture passed installation,
both recreations, persisted-state and Secure/HttpOnly probes, and exact cleanup
(exit 0). Production application and canonical-HTTPS restart checks remain pending
in the [deployment record](../DEPLOYMENT.md).

## First-install proxy trust

Root's [prepare-mautic.py](../runtime/prepare-mautic.py) implements the early PHP
configuration needed by Mautic 6's
[TrustMiddleware](https://github.com/mautic/mautic/blob/6.x/app/middlewares/TrustMiddleware.php)
and [ConfigAwareTrait](https://github.com/mautic/mautic/blob/6.x/app/middlewares/ConfigAwareTrait.php).
The ConfigAwareTrait/ParameterLoader path reads `parameters_local.php` before the
installer; an env-only `MAUTIC_TRUSTED_PROXIES` change is insufficient. The approved
first-install override is exactly these two lines, including a final newline:

<augment_code_snippet mode="EXCERPT">
````php
<?php
$parameters = ['trusted_proxies' => ['172.30.251.2']];
````
</augment_code_snippet>

- Creation is **first-install only**: `/srv/business-tools/mautic/config` must
  already be an empty real directory owned by `33:33`, mode `0700`. The helper
  does not mount storage, create the directory or repair ownership/modes.
- It writes a `33:33`, `0600` temporary file, flushes it, then publishes
  `parameters_local.php` atomically with an exclusive hard link, never replacing
  an existing name. The published file must be regular and single-linked; symlink
  paths, unsafe identities/modes and publication races fail closed.
- An existing byte-for-byte seed with the required identity/mode is accepted
  without writes, including after installation creates `local.php`. The helper
  neither reads/edits `local.php` nor parses/evaluates PHP.
- **Existing `local.php` (or any other config entry) without the known seed, or
  any changed `parameters_local.php`, is preserved and refused for manual review.**
  There is no automatic deletion or migration procedure. Preserve operator settings
  and arrange a reviewed resolution; do not empty the directory or widen trust to
  bypass the guard. A failure reports only a fixed manual-review message, not PHP
  contents or exception details.
- Bootstrap invokes the helper after disk preparation, while the **whole shared
  project is stopped** and the maintenance and exclusive data locks are held.
  Every systemd start runs only `prepare-mautic.py --check` under the shared data
  lock after the disk check. Restarts validate; they never seed or repair config.

The sole trusted address is Caddy's `172.30.251.2`, **not** the whole `/29`, all
private ranges, wildcard addresses or shared-edge peers. This implementation still
needs selected-image runtime validation of installer HTTPS detection, cookies,
redirects and generated links through the canonical IAP route. It is not a passing
live TLS/installer test; public DNS and Caddy/ACME certificate issuance are required.

## Exact environment contract

Root writes `0600`, root-owned files in `/run/business-tools` (`0700`). Generate
DB credentials out of band; none belong in Terraform, plans/state or source.
Files use raw `KEY=value` lines, without outer quotes, `export` or inline comments.
Reject NUL/CR/LF in values. Raw env format passes dollar signs and quotes literally.
Do not print environments or resolved production Compose configuration.

| File | Required secret keys | Constraints |
| --- | --- | --- |
| `/run/business-tools/mautic.env` | `MAUTIC_DB_PASSWORD` | Matches `MARIADB_PASSWORD`; app user/database `mautic`, host `mautic-mariadb`, default port 3306 are in Compose. |
| `/run/business-tools/mautic-db.env` | `MARIADB_PASSWORD`, `MARIADB_ROOT_PASSWORD` | Two different strong generated passwords; neither root credential nor this file goes into the app containers. |

Use at least 32 random bytes encoded as hex or unpadded base64url for each DB
password. Do not enable empty/random-root-password modes (the latter prints the
generated root password). DB init variables only apply to **new** databases;
credential rotation must update the actual SQL accounts and then all clients.
The normal Mautic user receives grants only on its application schema.

Only these two env files are needed here. SMTP/integration credentials are optional
until configured. Prefer the private installation/configuration UI so Mautic stores
operator choices in its protected config. If using `MAUTIC_MAILER_DSN`, place the
credential-bearing DSN in `mautic.env`, percent-encode user/password components and
verify TLS with your provider. Public URL, SMTP non-secret configuration, proxy
trust and optional queue choices belong in Compose or Mautic's operator-managed
configuration, not in Terraform secrets. Never put the admin password in CLI argv.

## Cron and optional workers: preserve Mautic defaults

The official `DOCKER_MAUTIC_ROLE` values are `mautic_web`, `mautic_cron`, and
`mautic_worker`; **not** commands such as `cron` passed to the image. We retain the
entrypoints. Cron and workers wait until persisted `local.php` has both `db_driver`
and `site_url`. The web healthcheck accepts installer redirects deliberately so
installation can proceed; it does **not** certify readiness for public access.

`mautic-cron` uses upstream's schedule: segments at minutes 0/15/30/45, campaign
updates at 5/20/35/50, campaign triggers at 10/25/40/55. Other jobs in its template
(email fetching, imports, exports, webhooks, IP lookup, integrations, cleanup) are
commented out. Enable only jobs you actually need after review. No cleanup or
retention policy is silently enabled here. Do not add duplicate host cron jobs.

**Worker profile `mautic-workers` is opt-in.** Mautic 6's application defaults are
`messenger_dsn_email=sync://`, `messenger_dsn_hit=sync://`, and no failed transport.
The Docker example changes those to Doctrine async queues; we do **not** copy that
behavioral override. A worker cannot consume `sync://`. Default installation
therefore runs web + cron + MariaDB, without an error-looping worker container.

If the operator deliberately chooses async delivery:

1. Configure supported async email/hit/failed transports through Mautic's supported
   configuration, shared by all three roles. For a local DB-backed queue, upstream
   documents Doctrine; no extra queue broker image is necessary.
2. The corresponding environment names, if using a reviewed Compose override,
   are `MAUTIC_MESSENGER_DSN_EMAIL`, `MAUTIC_MESSENGER_DSN_HIT` and
   `MAUTIC_MESSENGER_DSN_FAILED`. Do not persist sample DSNs over existing choices.
3. Enable `--profile mautic-workers` consistently in root's merged Compose/systemd
   lifecycle. Never enable all profiles blindly. The official worker uses foreground
   supervisor to run `php /var/www/html/bin/console messenger:consume email`, `hit`,
   and `failed` as www-data. All three transports must be consumable unless you
   deliberately configure the corresponding worker count to zero.
4. Worker count controls are `DOCKER_MAUTIC_WORKERS_CONSUME_EMAIL`,
   `DOCKER_MAUTIC_WORKERS_CONSUME_HIT`, `DOCKER_MAUTIC_WORKERS_CONSUME_FAILED` (upstream
   default two each). No capacity override is imposed here. Review VM resources,
   send one approved message and verify delivery, queue drain, retries and failures.

Changing transport semantics needs explicit operator approval, not a convenience
fix to get a green smoke test. SMTP normally needs outbound 465/587 plus DNS, not
an assumed unrestricted GCE port 25. Configure SPF/DKIM/DMARC, bounce handling,
unsubscribe behavior and consent before sending real campaigns.

## Secure installation and public activation

1. Root Caddy's public readiness must default **false**, blocking **all** public
   application paths across all five apps. Verify externally that `/`, `/s/`,
   `/installer`, tracking, forms and arbitrary paths do not reach Mautic. The
   private Invoice renderer exception is confined to the invoices host and grants
   no access to Mautic. Do not rely on an installer-path deny list or the web
   container's healthcheck.
2. Use root's authenticated IAP loopback proxy/tunnel, retaining the canonical
   HTTPS marketing hostname and secure cookies. Complete the web installer and
   create the administrator privately. Do not enable `DOCKER_MAUTIC_LOAD_TEST_DATA`.
   Set the persisted site URL to the same HTTPS URL as `MAUTIC_SITE_URL`.
3. Validate the [first-install proxy-trust contract](#first-install-proxy-trust),
   including early installer requests, with the selected image. Confirm HTTPS
   detection, secure cookies, redirects and generated links through IAP. The narrow
   seed and dedicated proxy route are implemented, but this live smoke test is
   still pending. Preserve/refuse conflicting existing settings for manual review;
   never use wildcard, subnet-wide or shared-edge trust as a workaround.
4. Verify authenticated login, an unauthenticated installer request cannot create
   a second admin/reinstall, and web/cron see the same persisted config. Confirm
   segment and campaign updates actually run. Test upload/read, SMTP, and an
   approved test contact/campaign using supported UI/API writes (not direct SQL).
5. Keep worker defaults or explicitly complete the async checks above. Validate a
   controlled restart and restoration of DB/config/media; inspect sanitized logs
   rather than exposing credentials. Take a restorable backup before migrations.
6. Only then allow the operator to enable root's public readiness after all five
   apps pass activation checks and the whole shared stack passes an isolated
   restore drill. Public forms, tracking and provider callbacks inherently require
   ingress; verify them during a controlled activation window. Protect the admin UI
   separately if required; don't accidentally block intended public forms.

The upstream web entrypoint runs migrations on installed databases. Upgrade all
roles together and review migration behavior for the exact digest first. This is
single-node infrastructure: resource exhaustion or any container exit can stop the
whole merged project. Healthchecks do not restart unhealthy-but-running processes.

## Official sources reviewed (2026-09-20)

- Docker source revision `2707c46b950d6e0f861680a5f42bcc7bbb8572aa`:
  [README: image families/roles](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/README.md),
  [Dockerfile: exact volumes](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/Dockerfile),
  [entrypoint](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/common/docker-entrypoint.sh),
  [ownership checks](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/common/startup/check_volumes_exist_ownership.sh),
  [installation wait](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/common/startup/wait_for_mautic_install.sh),
  [cron](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/common/templates/mautic_cron),
  [workers](https://github.com/mautic/docker-mautic/blob/2707c46b950d6e0f861680a5f42bcc7bbb8572aa/common/templates/supervisord.conf).
- [Application's actual queue defaults](https://github.com/mautic/mautic/blob/6.x/app/bundles/MessengerBundle/Config/config.php),
  [environment processing](https://github.com/mautic/mautic/blob/6.x/app/config/parameters.php),
  [installation](https://docs.mautic.org/en/6.0/getting_started/how_to_install_mautic.html).
- Mautic 6.0.9 [compiled session-cookie configuration](https://github.com/mautic/mautic/blob/6.0.9/app/config/config.php)
  and [supported CLI installer options](https://github.com/mautic/mautic/blob/6.0.9/app/bundles/InstallBundle/Command/InstallCommand.php)
  used by the isolated cache regression.
- [Debian Bookworm www-data identity](https://sources.debian.org/data/main/b/base-passwd/3.6.1/passwd.master),
  [MariaDB image](https://github.com/MariaDB/mariadb-docker/blob/master/11.4/Dockerfile),
  [MariaDB ownership/init](https://github.com/MariaDB/mariadb-docker/blob/master/docker-entrypoint.sh),
  [healthcheck](https://github.com/MariaDB/mariadb-docker/blob/master/main/healthcheck.sh).
- [Compose services/env/bind semantics](https://docs.docker.com/reference/compose-file/services/).

From the repository root, run the focused seed tests and both contract suites:

<augment_code_snippet mode="EXCERPT">
````sh
.venv/bin/python -B -m unittest discover -s infrastructure/business-tools/tests -p test_mautic_setup.py -v
.venv/bin/python -B -m unittest discover -s infrastructure/business-tools/mautic/tests -v
.venv/bin/python -B -m unittest discover -s infrastructure/business-tools/tests -p test_stack.py -v
````
</augment_code_snippet>

The seed suite contains **22 isolated tests** covering exact creation, no-write
reruns/checks, refusal/preservation of existing settings, modes/owners/links,
publication races and secret-safe errors. Only fixture numeric ownership is
simulated; filesystem operations use temporary directories. Fragment/stack checks
cover roles, mounts, proxy isolation, seed/address agreement, lock/check wiring and
unchanged defaults. Optional Compose validation uses synthetic inputs; the optional
Caddy adapter test uses an already-local image without network. Record skips too.
These checks do not exercise Mautic/PHP or prove selected-image compatibility.
The integration has not yet been runtime-tested: actual installation, early proxy
trust, valid TLS/IAP, SMTP and restoration remain operator-approved isolated smoke
and restore gates. No deployment or passing live test is claimed.