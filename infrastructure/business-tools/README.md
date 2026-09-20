# Business tools: shared VM operator guide

This root owns one merged Compose deployment and its Terraform/runtime lifecycle.
It is configuration, **not evidence of a live deployment or passing runtime tests**.
`infrastructure/ai-gateway` and `infrastructure/raise-crm` remain separate and
untouched. The older `infrastructure/agentic-social` location contains only a README
redirect; its superseded standalone deployment sources have been removed. Do not
reuse another stack's state or infer deployed state from this directory.

## Topology and defaults

| Application folder | Application | Canonical HTTPS hostname |
| --- | --- | --- |
| [agentic-social](agentic-social/README.md) | Postiz + PostgreSQL, Redis, Temporal and its separate PostgreSQL | `social.makemoredigital.com` |
| [mautic](mautic/README.md) | Mautic web/cron + MariaDB; workers opt-in | `marketing.makemoredigital.com` |
| [actual-budget](actual-budget/README.md) | Actual Budget (budgeting, not full business accounting) | `budget.makemoredigital.com` |
| [invoice-ninja](invoice-ninja/README.md) | Invoice Ninja Debian/PHP-FPM + Nginx, MariaDB and dedicated Redis | `invoices.makemoredigital.com` |
| [observability](observability/README.md) | Grafana + private Prometheus/Node Exporter | `metrics.makemoredigital.com` |

[Pipeboard](../pipeboard/README.md) is the selected paid **hosted** Google/Meta ads
management service, not a sixth VM service or a replacement for Postiz publishing.
Account, payment, OAuth and ad-account connection remain separate human steps;
selection and documentation do not establish a subscription or connection.

Defaults are `business-tools`, **e2-standard-4: 4 vCPUs / 16 GB RAM**, London
`europe-west2` / `europe-west2-b`, Ubuntu 24.04 amd64, a disposable 50 GB balanced
boot disk and a separate retained 150 GB `pd-balanced` data disk. Persistent data
lives at `/srv/business-tools`; staged non-secret sources at `/opt/business-tools`.
The VM has deletion protection; the data disk and secret container have Terraform
`prevent_destroy`. These protections are not backups or high availability.

There is a dedicated VPC/subnet, static external IPv4 and VM service account.
Only Caddy publishes web ports; databases, Redis, Temporal and metrics backends
are not public. SSH is restricted to IAP's source range with OS Login; project SSH
keys and serial-port access are disabled. Containers are blocked from metadata
credentials; the host fetches secrets using its narrowly scoped VM identity.

`public_enabled` defaults to **false**: public TCP 80/443 are still open for
Caddy/ACME, but external HTTPS application requests return 503 on all paths. The
only port-443 exception while closed is inside the invoices host: Caddy matches
the exact transport peer `remote_ip 172.30.252.3` for the private renderer route.
Forwarded headers cannot grant that exception, and it does not open other origins.
A loopback-only 8443 listener provides operator setup. `dns_managed_zone = null`
leaves DNS external; `operator_members = []` grants nobody access automatically.
Omit optional settings to inherit defaults; preserve deliberate existing overrides
and persisted choices.

Two dedicated internal bridges implement the integration paths; each has only
Caddy and its named application peer:

| Network / reserved subnet | Caddy | Application peer / routing contract |
| --- | --- | --- |
| `mautic-proxy` / `172.30.251.0/29` | `172.30.251.2` | `mautic` at `172.30.251.3`; Caddy uses `mautic-web:80`, whose alias exists only on this bridge. Web retains `edge` for egress, not trusted ingress. |
| `invoice-render` / `172.30.252.0/29` | `172.30.252.2`, alias `domains.invoices` (default `invoices.makemoredigital.com`) | `invoice-app` at `172.30.252.3` reaches canonical HTTPS assets on 443 with the real hostname/certificate. PHP stays off `edge`. |

These are implemented routing/configuration contracts, **not proof of working TLS
or a complete PDF render**. Public DNS and successful Caddy/ACME issuance are still
required; the private alias does not issue a certificate or disable validation.

This is **not free hosting**: budget for VM, both disks, external IPv4, snapshots,
state storage, egress, DNS and secret operations, plus any SMTP/social/payment
services. Set billing alerts; resource limits do not guarantee adequate capacity.

## Prerequisites and image release gate

- Use an existing billed project with quotas and externally managed API enablement:
  Compute Engine, IAM, IAP, OS Login, Secret Manager, Cloud Resource Manager and
  Cloud Storage; enable Cloud DNS too if managing records here. This module does
  not enable/disable APIs or create the project/state bucket.
- Arrange deployer permissions, backend access and explicit operator IAP/OS Admin
  Login access. Optional `operator_members` grants instance-scoped IAP SSH/OS Admin
  Login and service-account-user on this VM identity, not general project discovery
  rights. Separately authorize the operator who uploads secret versions.
- `versions.tf` requires **Terraform >= 1.9.0**, Google provider `~> 7.0`;
  OpenTofu >= 1.11 supports the mock tests (**tested tooling version: 1.12.6**).
  Use authenticated Google CLI/ADC without exporting credentials into commands.
  Host bootstrap installs Ubuntu packages and requires **Compose >= 2.30.0** for
  raw env files; it fails rather than silently downgrading that contract.
- Use an existing access-reviewed GCS backend bucket and an **isolated** prefix
  (`business-tools/state` by default). Never share another stack's prefix or migrate
  its state into this root. Protect backend configuration and saved plans.
- Before deployment, check both reserved `/29` ranges above against host, Docker,
  VPC and VPN routes for collisions. Do not renumber one contract alone: Compose
  subnets/addresses/aliases, the Mautic seed, Caddy's renderer matcher and tests must
  stay coordinated. An existing Mautic seed is not automatically migrated; any
  conflict requires manual review while preserving persisted settings.

`project_id` and the complete `images` map are required. Every image must be an
explicit, verified registry reference ending in `@sha256:` plus 64 lowercase hex
characters. [terraform.tfvars.example](terraform.tfvars.example) deliberately uses
**invalid placeholders** pending release selection and smoke testing. Terraform
only checks syntax; it does not pull images or verify provenance/compatibility.
Never substitute synthetic mock-test hashes or floating tags.

| Required map key | Image/release contract to verify and pin |
| --- | --- |
| `postiz` | `ghcr.io/gitroomhq/postiz-app`, supported Temporal-based 2.12.0+ release |
| `postgres` | `postgres`, 17 Bookworm, Postiz data layout |
| `redis` | `redis`, 7.2 Bookworm; separate Postiz and Invoice instances |
| `temporal` | `temporalio/auto-setup`, 1.28.1 family, not server-only/tools |
| `temporal_postgres` | `postgres`, 16 Bookworm, separate Temporal database |
| `mautic` | `mautic/mautic`, 6 Apache/Bookworm; same digest for all roles |
| `mariadb` | `mariadb`, 11.4 LTS; separate application instances |
| `actual` | `actualbudget/actual-server`, 26.9.x Debian contract, UID/GID 1001 |
| `invoice_ninja` | `invoiceninja/invoiceninja-debian`, v5.13.x; old non-Debian image is not interchangeable |
| `invoice_nginx` | `nginx`, 1.30.x Alpine |
| `grafana` | `grafana/grafana`, 12.4.x |
| `prometheus` | `prom/prometheus`, 3.x |
| `node_exporter` | `prom/node-exporter`, 1.x |
| `caddy` | `caddy`, reviewed release compatible with the root Caddyfile |

These are source contracts, not certified patch combinations. Verify amd64 support,
entrypoints, numeric identities, declared volumes and migration behavior against
the **selected digests**. Follow each linked app checklist on an isolated test
installation before launch; do not discover compatibility by migrating live data.

## Prepare, review, then apply manually

These are operator instructions, not an automated deployment procedure.

1. In this directory, copy `backend.hcl.example` and `terraform.tfvars.example` to
   their corresponding local filenames **only if absent**. Confirm the project,
   bucket/prefix and verified image pins; deliberately choose operator/DNS options.
   Keep the public gate closed. No credentials, bootstrap JSON or secret versions
   belong in Terraform variables, templates, metadata, plans or state.
2. Initialize and prepare a saved plan; protect the local plan file:

<augment_code_snippet mode="EXCERPT">
````sh
tofu init -backend-config=backend.hcl
tofu validate
tofu plan -out=business-tools.tfplan
tofu show business-tools.tfplan
````
</augment_code_snippet>

3. Review every resource, IAM grant, cost and replacement/deletion action. Stop if
   anything targets another stack or existing user data unexpectedly. Have the
   authorized operator **manually apply only that reviewed saved plan**; never use
   auto-approval. Terraform commands may be substituted for OpenTofu consistently.
4. Read safe outputs: `static_ip`, `instance_name`, `zone`, `project_id`,
   `bootstrap_secret_id`, `iap_ssh_command` and `image_references`. `public_urls`
   is empty while closed and only describes intended URLs when open, not readiness.
   Point all five public A records at `static_ip` (or review the optional Cloud DNS
   records). Resolve conflicting records and verify certificate issuance before
   private browser setup. Public DNS does not require opening application routes.

### Bootstrap credentials: out of band only

For a **new, empty deployment**, choose an existing operator-owned private directory
outside the repository (0700). Run the generator from the repository root, replacing
the example path with a new file in that directory:

<augment_code_snippet mode="EXCERPT">
````sh
.venv/bin/python infrastructure/business-tools/scripts/create-bootstrap.py \
  --output /absolute/private/directory/business-tools-bootstrap.json
````
</augment_code_snippet>

It prompts privately for Invoice administrator email/Grafana username, generates
credentials and writes a new 0600 file without printing values or overwriting an
existing file. Keep a secured recovery copy. The root schema in
`runtime/fetch-secrets.py` is authoritative: nested `postiz`, `mautic`,
`invoice_ninja`, `grafana` sections, not a flat copy of app env-variable tables.
Actual has no bootstrap JSON section: set its password in the private UI.

After Terraform creates the empty secret container, upload the file out of band:

<augment_code_snippet mode="EXCERPT">
````sh
gcloud secrets versions add business-tools-bootstrap-json --project PROJECT_ID \
  --data-file=/absolute/private/directory/business-tools-bootstrap.json
````
</augment_code_snippet>

Only identifiers/paths belong in shell arguments. An approved secret manager may
instead stream directly to stdin with `--data-file=-`; never echo literals, expand
secret shell variables into argv, print the payload, or enable shell/HTTP debugging.
Protect editor backups and local recovery files too.

Until a valid accessible version exists, secret preparation fails closed and
systemd retries every 30 seconds. Each start reads `latest` and writes allowlisted
root-owned 0600 env files under 0700 `/run/business-tools`. This is not automatic
credential rotation: changing DB init passwords does not update existing SQL users;
changing initial admin values does not reset existing accounts. Preserve Invoice
APP_KEY, Grafana encryption key, database credentials and persisted configuration.
Never rerun the generator as an upgrade or recovery shortcut.

## Private initialization and activation

1. Keep `public_enabled = false`. From outside the tunnel, verify all five hosts'
   setup/API/media/arbitrary paths cannot reach apps, not just their login pages.
2. Use the output IAP SSH command with a **loopback-only** local forward. Substitute
   the output project/zone; the following transports local 8443 to VM loopback 8443:

<augment_code_snippet mode="EXCERPT">
````sh
gcloud --project PROJECT_ID compute ssh business-tools --zone ZONE \
  --tunnel-through-iap -- -N -o GatewayPorts=no -L 8443:127.0.0.1:8443
````
</augment_code_snippet>

3. The browser must still visit each **canonical HTTPS origin on port 443**, not
   localhost or a URL with `:8443`. Caddy's private listener routes by hostname;
   preserve TLS SNI, HTTP Host, secure cookies, redirects and certificate validation.
   The SSH forward alone is insufficient: arrange an approved client-side TCP
   mapping from each canonical host's port 443 to `127.0.0.1:8443`. For a browser,
   this can be temporary local hostname resolution plus a loopback-only TCP relay
   from local 443 to the tunnel, without TLS termination. Hosts-file entries alone
   do **not** remap ports. Use a platform-reviewed setup, remove it afterwards, and
   never disable TLS checks or weaken app URLs/cookies. For command-line checks a
   client's connect-to mapping can preserve the origin, but is not browser setup.
4. Initialize **all five** apps before any public activation:
   - **Postiz:** privately create the first account; verify Temporal namespace and
     workflows. `DISABLE_REGISTRATION=true` allows the first account but blocks
     subsequent signup and OAuth/OIDC login. Test API-level second-signup rejection.
   - **Mautic:** complete the installer/admin account and canonical site URL; verify
     reinstall is blocked and cron sees persisted configuration. First-install
     proxy trust is implemented by `prepare-mautic.py`: it seeds
     `parameters_local.php` with only Caddy's `172.30.251.2` before installer
     middleware runs (see the [app contract](mautic/README.md#first-install-proxy-trust)).
     Existing `local.php` without the exact known seed, or a changed override,
     fails closed for manual review without overwriting settings. Verify HTTPS
     detection, secure cookies and redirects with the selected image through IAP;
     never substitute wildcard, private-range or shared-edge trust.
   - **Actual:** set the first server password, create a test budget through its UI,
     verify login and denial of unauthenticated budget reads.
   - **Invoice Ninja:** verify the generated initial account, no unauthenticated
     setup/account takeover, client portal permissions, workers/scheduler, Redis
     queue/cache/sessions and local PDF rendering with logos/fonts. The implemented
     `invoice-render` path lets PHP reach the canonical invoices origin on 443
     while the public gate stays closed, independently of the laptop tunnel. Test
     real certificate validation and the full PDF path with selected images;
     routing and adapter checks alone prove neither. Do not open public setup,
     disable TLS verification or switch to a hosted renderer to pass a test.
   - **Grafana:** verify initial admin login, anonymous access and signup disabled,
     provisioned Prometheus datasource and both scrape targets. Save a dashboard.
   For every app, verify authentication restrictions, persistence across a controlled
   restart, and the linked app-specific checks. HTTP health alone is insufficient.
5. Mautic defaults to synchronous transports: web + cron + DB, **no worker profile**.
   Async email/hit/failed transports and `mautic-workers` need explicit approval,
   shared configuration and consistent root lifecycle changes. The wrapper clears
   `COMPOSE_PROFILES`; exporting it is not activation. Do not duplicate cron/workers.
6. SMTP, social OAuth, public callbacks and campaign/invoice delivery are **not
   activated by deployment**. Obtain provider approvals/scopes and exact callback
   registrations; configure TLS mail submission, SPF/DKIM/DMARC and consent before
   approved test sends. Public callbacks/media require a controlled activation
   window. Current `extra_env` support is limited to the root allowlist: Mautic mail
   lives in its private admin UI; Grafana SMTP and Postiz nodemailer settings are
   not fully wired here. App guides describe optional contracts, not implemented
   root support. Any allowlist/configuration extension needs separate review/tests.
7. After private setup, digest smoke tests and a successful isolated restore drill,
   approve public activation. Set `public_enabled = true`, review and manually apply
   the plan, then explicitly rerun the metadata startup script as described below.
   Recheck registration/install restrictions and approved end-to-end integrations.

## Shared lifecycle, maintenance and logs

`business-tools.service` is the sole lifecycle owner. Its foreground merged Compose
uses `--abort-on-container-exit`: **one container exit stops all applications**, then
systemd restarts the project. Unhealthy-but-running containers do not trigger that
flag. Do not start detached app fragments or a second Compose owner. Plan controlled
downtime for restarts, backups and updates; stop timeout is 4200 seconds because
Invoice workers alone can take an hour to shut down.

Startup stages root-owned sources, verifies the exact data disk/mount, prepares
missing directories, prepares the first-install Mautic proxy seed, installs units
and starts the stack. Bootstrap calls `prepare-mautic.py` after disk preparation,
with the whole project stopped and the maintenance and exclusive data locks held.
Only an empty config directory owned by `33:33`, mode `0700`, can receive a new
seed; publication is atomic/exclusive, never an overwrite. An exact existing seed
is validated without writes. Each systemd start uses only `--check` under the
shared data lock; it never seeds on restart. An installed config without the known
seed, or a changed override, requires manual review, not deletion, automatic
migration or ownership repair.
Existing data owners, contents and operator settings are preserved by host
preparation; there is no recursive ownership reset. Image entrypoints may still
migrate/repair their own data, so verify their behavior before upgrades. Never
manually format/chown data to work around a failed guard.

Image/template/public-gate updates change `metadata["startup-script"]`; applying
that metadata **does not execute it**. After backup and explicit maintenance
approval, rerun the **GCE metadata startup script** (or deliberately reboot) so it
restages current assets and runs bootstrap. Running only the old staged bootstrap
does not fetch new templates. Stage/bootstrap/backup scripts coordinate locks;
manual maintenance must also use `/run/business-tools-maintenance.lock`, stop via
systemd before taking the exclusive data lock, and release the data lock before
starting. Never bypass these checks or reset persisted choices on reruns.

Keep logs private: initialization, OAuth, invoice/payment/reset URLs and financial
content can contain secrets. The application unit suppresses Compose stdout/stderr;
Docker local logs are bounded to 10 MB × 3 per container, **not guaranteed sanitized**.
Caddy has no access logs and filters request headers/URIs; Invoice Nginx disables
access logging. Invoice application file logs additionally use the installed host
logrotate policy, whose daily rotation does not bound growth between runs and whose
`copytruncate` can lose lines. Do not dump container environments, resolved Compose
config, secret files or raw logs into tickets/chat. Prefer state/exit-code/count-only
checks; inspect necessary diagnostics through an approved private workflow.

## Backups and recovery limits

- The timer runs a **cold backup daily at 03:00 UTC plus up to 15 minutes jitter**;
  missed runs are not caught up at boot (`Persistent=false`). It requires an active
  stack, stops the whole project, verifies successful shutdown/no remaining project
  containers, archives, then attempts restart even after failure. Allow shutdown
  plus full compression time, not a short fixed outage.
- Completed 0600 archives in `/srv/business-tools/backups` contain the entire data
  tree except backups themselves, plus `/run/business-tools` runtime credentials,
  preserving numeric owners/ACLs/xattrs. Incomplete files are not published. Narrow
  cleanup removes completed matching archives **older than 14 days by age**, not
  necessarily after 14 successful backups. Monitor completion, restart and capacity.
- These archives are on the **SAME disk as the source data**, contain secrets, and
  provide no off-host disaster recovery. Plan an approved protected off-host copy
  and access/retention policy separately; none is implemented here.
- GCE snapshots have a separate daily **04:00 UTC** schedule and 14-day retention
  policy, keeping auto snapshots on source-disk deletion. They are **crash-consistent
  and independent** of the cold backup: they neither wait for nor guarantee backup
  completion. A long shutdown/backup can overlap them. `/run` keys are not on the
  data disk except when included in a completed archive; retain secured keys too.
- Before launch and upgrades, restore into isolated storage/compute with public
  ingress closed and outbound delivery controlled. Preserve numeric owners and
  matching DB/config/media/keys; test every app and recovery time before promoting
  anything. Never extract over live data. Archive compression checks and snapshots
  are **not proof of a working restore**; none is claimed here. Schema migrations
  can make image-only rollback unsafe.

Observability is also on this VM. Node Exporter deliberately lacks host filesystem
capacity/network and per-app metrics; no off-VM failure alerts are provided.
Arrange disk-space, backup-result and external health monitoring separately.

## Local validation versus launch evidence

From the repository root, run the existing isolated tests with its virtualenv:

<augment_code_snippet mode="EXCERPT">
````sh
.venv/bin/python -m unittest discover -s infrastructure/business-tools/tests -v
.venv/bin/python -m unittest discover -s infrastructure/business-tools/agentic-social/tests -v
.venv/bin/python -m unittest discover -s infrastructure/business-tools/mautic/tests -v
````
</augment_code_snippet>

Run native mock plans in a **fresh disposable copy of this module's reviewed source**,
without local tfvars, backend files, state, plans or credentials. From that copy:

<augment_code_snippet mode="EXCERPT">
````sh
tofu init -backend=false
tofu validate
tofu test -filter=tests/deployment.tftest.hcl
````
</augment_code_snippet>

Provider installation may require registry access; mock tests do not contact live
cloud APIs or apply resources. Python tests cover synthetic secrets, temporary
filesystem/maintenance behavior and rendered configuration. In particular,
`tests/test_mautic_setup.py` contains **22 isolated tests** for exact seed creation,
read-only reruns/checks, preservation/refusal, filesystem safety and quiet errors.
`tests/test_stack.py` covers the two-peer subnet/address/alias contracts, startup
lock/check wiring and closed/open Caddy gate structure; its optional Caddy adapter
check verifies the transport-peer matcher survives adaptation. Mautic's fragment
tests also cover the proxy network and unchanged role/queue defaults.

Some checks skip without Terraform/OpenTofu or Compose; optional Caddy adaptation
uses an already-local image without network. Record passes **and skips**. These
are not full application runtime, live TLS/IAP, SMTP/OAuth, backup/restore or
deployment tests. The integration fixes have **not yet been runtime-tested**;
selected-image smoke tests (including early Mautic HTTPS and full Invoice PDF
rendering) and an isolated restore drill remain mandatory launch gates. Update
relevant tests and rerun these commands whenever runtime/configuration contracts
change.

Local verification on 2026-09-20: **95 Python tests passed, no skips**, including
three layout regressions, the 22 Mautic seed tests, merged Compose validation and
offline Caddy adaptation;
OpenTofu formatting/validation and **19 mocked plan cases passed**. This records
source-level checks only. Image references remain invalid placeholders pending
release selection; no cloud apply, public activation or Pipeboard connection was
performed.