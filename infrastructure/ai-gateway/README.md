# AI gateway: Bifrost on GCP

Single `e2-medium` VM for `llms.makemoredigital.com`: project `makemoredigital2025`, region `europe-west2`, zone `europe-west2-b`.
Deployed on 2026-09-19, including the guarded public model catalog. Earlier private admin login
and public HTTPS boundaries were verified; public TLS checks and the post-deployment plan for
the latest catalog rollout are still pending results.
See [deployment status](DEPLOYMENT.md) for instance details and secure credential retrieval.
Optional Google-protected browser administration is described in [ADMIN.md](ADMIN.md).

## Ownership and architecture

Deliberately a **root-repository-owned workspace coordinator folder**, not a new nested Git repository.
It owns independent state (`ai-gateway/state`), VPC/subnet, firewalls, static IPv4, VM/service account,
resource-scoped IAM, retained data disk, snapshot schedule and bootstrap secret container.
It must not change Studio resources, state, networking, IAM or project API lifecycle.
Optional DNS creates only an A record in an explicitly selected existing zone.

Bifrost uses host networking, binding only `127.0.0.1:8080`; Caddy serves public HTTP/HTTPS on ports 80/443.
Public model discovery goes through a narrow Python guard on `127.0.0.1:9092`, never directly to Bifrost.
SSH is IAP-only. Bifrost's explicit `1000:1000` user override requires an image compatible with UID/GID 1000.
The Ubuntu boot disk is disposable; `/srv/ai-gateway` is the retained data disk.
VM deletion protection and data-disk/secret `prevent_destroy` are intentional safeguards.
This is non-HA and not a DoS/WAF replacement. Gateway budgets are not hard billing ceilings;
provider prices, capacity and customer quotes require later benchmarks and measured usage.

## Prerequisites

- Prefer **OpenTofu >= 1.9** (Terraform-compatible syntax); **OpenTofu 1.12** for mock-provider tests.
  Install `gcloud`; authenticate CLI and application-default credentials through supported local flows.
- Externally enable Compute Engine, IAM, IAP, OS Login and Secret Manager APIs:
  `compute.googleapis.com`, `iam.googleapis.com`, `iap.googleapis.com`, `oslogin.googleapis.com`,
  `secretmanager.googleapis.com`; optionally `dns.googleapis.com`. This module does not enable/disable APIs.
- Verify an **existing** GCS bucket/access externally; the example `makemoredigital2025-tfstate` may not exist.
  Review bucket access, versioning and retention; never reuse Studio's state prefix.
- The deployer must be able to create/manage the listed Compute resources, service account,
  Secret Manager container and secret IAM; attach/act as the VM service account; and read/write/lock
  backend objects. If enabled, it also needs DNS record permissions and authority to set instance,
  IAP and service-account IAM memberships. Optional operator roles do not grant deployment rights.
- Example variables pin Bifrost **v2.2.1** and Caddy **2.11.2-alpine** by verified
  multi-platform digest. Review release/security notes and test before upgrading.
- `operator_members` defaults empty. Opting in grants IAP tunnel access and OS
  Admin Login on this instance, plus Service Account User on its dedicated account.
  `gcloud` additionally needs `compute.projects.get`: if existing organization access is insufficient,
  arrange project `roles/compute.viewer` or a narrower approved grant externally. External users may need
  organization-level `roles/compute.osLoginExternalUser`. These are not auto-granted.

## Prepare, review, then apply manually

Run from the repository root. Copy examples only when local files do not exist; preserve existing choices.
Keep credentials out of variables, metadata, plans and state. Protect local backend configuration and saved plans.

<augment_code_snippet mode="EXCERPT">
````sh
cd infrastructure/ai-gateway
cp -n backend.hcl.example backend.hcl
cp -n terraform.tfvars.example terraform.tfvars
````
</augment_code_snippet>

Edit both copies: verify bucket/prefix, set verified image digests, and choose optional operators/DNS deliberately.
Defaults select the machine, region, zone and hostname above; no DNS operations by default.

<augment_code_snippet mode="EXCERPT">
````sh
tofu init -backend-config=backend.hcl
tofu plan -out=gateway.tfplan
````
</augment_code_snippet>

**REVIEW** targets, IAM, costs, retention and no Studio changes. Only after explicit approval, apply manually:

<augment_code_snippet mode="EXCERPT">
````sh
tofu apply gateway.tfplan
````
</augment_code_snippet>

### Supply the bootstrap secret out of band

Apply creates `ai-gateway-bootstrap-json` **without a version**. Systemd intentionally fails closed,
retrying every **30 seconds** until a valid version is accessible. No providers or virtual keys by default.
Supply a JSON object with exactly these four string keys (all `BIFROST_`-prefixed):

| Key | Requirement |
| --- | --- |
| `BIFROST_ADMIN_USERNAME` | 1–128 characters |
| `BIFROST_ADMIN_PASSWORD` | At least 32 random characters |
| `BIFROST_ENCRYPTION_KEY` | At least 32 random characters; permanent DB key |
| `BIFROST_SETUP_TOKEN` | At least 32 random characters |

Every value must use only `[A-Za-z0-9_./+=:@-]`. Strongly prefer independently password-manager-generated
**32 random bytes encoded as base64url** for each secret value. Preserve the encryption key permanently
with the database/backups: **do not rotate it blindly**; rotation needs a supported migration.

Use Secret Manager's console, or a local JSON file **outside this repository**, created with mode `0600`
in a restricted directory. The uploading identity needs version-add permission. Never pass JSON/secret values
inline, through arguments or stdout; never `cat` secrets. The path below is a placeholder, not a file to commit.

<augment_code_snippet mode="EXCERPT">
````sh
gcloud secrets versions add ai-gateway-bootstrap-json \
  --project=makemoredigital2025 \
  --data-file=/absolute/private/bootstrap.json
````
</augment_code_snippet>

The VM reads `latest` at service start into a root-only runtime environment file; versions do not hot-reload.
Systemd owns startup/retries. Docker restart policies must stay disabled to avoid bypassing mount/secret checks.

### DNS and private administration

Manually point the hostname's A record at `tofu output -raw static_ip`, unless
`dns_managed_zone` explicitly selects an existing Cloud DNS zone. Allow propagation
and Caddy certificate issuance; do not add an unsupported IPv6/AAAA destination.
Review/run `outputs.tf`'s `iap_ssh_tunnel_command` locally:

<augment_code_snippet mode="EXCERPT">
````sh
tofu output -raw iap_ssh_tunnel_command
gcloud --project makemoredigital2025 compute ssh ai-gateway \
  --zone europe-west2-b --tunnel-through-iap -- \
  -N -L 127.0.0.1:8080:127.0.0.1:8080
````
</augment_code_snippet>

Browse `http://127.0.0.1:8080` with the tunnel open. Use private setup/admin credentials; never expose/share them.

## Public API and client isolation contract

`runtime/Caddyfile` exposes this exact allowlist; no broad wildcards:

- `POST /v1/chat/completions`, `/v1/responses`, `/v1/embeddings`
- `POST /anthropic/v1/messages`
- `GET /v1/models` through the catalog guard, with no query string

### Guarded model discovery

`runtime/model-catalog.py` forwards only the caller's canonical `Authorization: Bearer sk-bf-*`
credential to the fixed Bifrost `/v1/models` endpoint. Exactly one Authorization header is required;
cookies, alternate key headers and query credentials cannot authenticate. The token format check
is **not proof of authenticity**. Query strings (including an empty `?`) are rejected with 400.

| Result | Public status |
| --- | --- |
| Missing or malformed canonical Bearer credential | 401 |
| Syntactically valid but invalid/revoked key, or any empty catalog | 403 |
| Valid key with no grants/models | 403, never a successful empty list |
| Valid key with a nonempty authorized catalog | 200, minimal model projection |
| Unavailable guard/upstream or invalid upstream response | 502 |

The guard validates and deduplicates model IDs and emits only `id`, `object: model` and
`owned_by` derived from the ID inside the list envelope. Provider-key IDs, key status,
upstream diagnostics and other metadata are not exposed; responses are not cached.
It loads, stores and uses **zero additional provider or admin credentials**.

**Pinned-image trust contract:** Bifrost, not the guard's syntax check, must authenticate the
virtual key and scope every nonempty `data` array to its saved grants. The deployed Bifrost
v2.2.1 pin is `sha256:a8942692af7b4b89196cd8fc33653b7353488dfd58b24078fe793b8574a8084b`.
The real pinned-image, multi-provider isolation proof passes; rerun it before every image upgrade.
A client may show explicit models or the provider catalog permitted by its **saved virtual key**,
not the global gateway/provider catalog. Discovery is not a substitute for inference authorization.

Historically, `/v1/models` was private and returned 404 at the public edge: this Bifrost release
can return 200 with `data: []` for invalid keys on an empty deployment. The deployed guard now
closes that gap by denying every empty catalog; never proxy this route directly to Bifrost.

### Inference and client setup

Inference routes admit only `Authorization: Bearer sk-bf-*`-shaped credentials: a format check,
not authenticity. Bifrost must validate the virtual key and enforce permissions. Caddy strips
`x-bf-*`, cookies and provider-key headers on inference requests. No raw-provider/direct-key bypass
or client routing/auth overrides. The catalog guard constructs its own fixed upstream request.
Stored-response GETs, threads, files, jobs, admin and MCP routes are not public.
Public `GET /healthz` returns Caddy `ok`, **not Bifrost readiness**.
Request bodies are capped at 16 MB. API keys belong on trusted servers, not in browser code;
browser CORS access is not configured. This small VM still needs external monitoring and abuse protection.

OpenAI-compatible SDKs use `base_url=https://llms.makemoredigital.com/v1`, securely supplied virtual keys and `provider/model`.
Anthropic SDKs use `base_url=https://llms.makemoredigital.com/anthropic`, native
Anthropic model format, and `auth_token` (Bearer), not a raw `api_key`/`x-api-key`:
only Authorization credentials are admitted at the edge. Never log client keys.

Privately configure providers, then issue a **separate virtual key per client/application/environment**
with an explicit provider key ID and model allowlist, never shared wildcard access across clients.
Test missing/invalid/unauthorized keys/models and fallback isolation: client A must never use client B's key.
No request/response content or log store; auth enforcement, disabled raw overrides and privacy guards
are deliberate gateway security settings, not general fixture/default changes. Verify the pinned image
honors them before serving traffic; provider retention is separate.

## Operations, backups and recovery

The catalog runs as the dedicated non-login local system account `ai-gateway-catalog`, using
the VM's system Python standard library without new dependencies. Its sandbox masks exactly
`/opt/ai-gateway`, `/srv/ai-gateway` and `/run/ai-gateway`, not all of `/run`.
`RestrictAddressFamilies=AF_UNIX AF_INET`, `IPAddressDeny=any`,
`IPAddressAllow=127.0.0.1/32`, `SocketBindDeny=any` and `SocketBindAllow=ipv4:tcp:9092`
restrict networking. Strict filesystem protection, private temporary files/devices, no new privileges
or capabilities, kernel/control-group protections, resource limits and disabled core/output logs remain.
The gateway uses `Wants`/`After`, **not `Requires`**, for the catalog service: its failure returns
catalog 502s without stopping inference. See [deployment status](DEPLOYMENT.md) for the observed
pre-exec/NSS and mount-namespace failures that led to this sandbox; OS Login causality is not proven.

Config is seeded only when absent, preserving dashboard edits. After applying template/metadata changes, rerun startup
on the VM (this **restarts** the gateway): `sudo google_metadata_script_runner startup`.
Manually reconcile security-policy changes in existing config; never blindly
overwrite dashboard state or assume a new seed updates it.

Safe VM status checks below avoid environment/config dumps and log content:

<augment_code_snippet mode="EXCERPT">
````sh
sudo systemctl is-active ai-gateway.service
sudo systemctl show ai-gateway.service ai-gateway-model-catalog.service \
  ai-gateway-iap-auth.service -p Id -p ActiveState -p SubState -p NRestarts
sudo systemctl list-timers ai-gateway-backup.timer --no-pager
````
</augment_code_snippet>

Never emit secrets/content: no `docker inspect`, environment dumps, `docker compose config` or verbose logs.
Online SQLite backups run at **03:00 UTC** under `/srv/ai-gateway/backups`; daily
crash-consistent disk snapshots follow at **04:00 UTC**. Both retain 14 days; SQLite pruning follows success.
Backups include committed WAL data, but separate databases are not one cross-database transaction.
Treat databases/backups/snapshots as sensitive plaintext; not all fields are encrypted. Restrict Secret Manager
access; retain encryption material separately from disk backups, preserving DB/key pairing and needed versions.

Recovery requires a reviewed procedure: stop the app (and instance for disk
replacement), preserve the failed disk, then restore an entire disk snapshot or
the selected SQLite backup. Never copy a live database. Handle stale `-wal`/`-shm`
files only while stopped under that procedure, never with blanket deletion.
SQLite-only backups exclude config/key material. Restore ownership and matching configuration/key;
test restore integrity and client isolation on an isolated target before reopening traffic.

## Local verification (no deployment)

Run these from the repository root. Tests remain isolated: no cloud writes or host database.
Backend-disabled initialization downloads provider binaries but does not access the state bucket.

<augment_code_snippet mode="EXCERPT">
````sh
.venv/bin/python -B -m unittest discover -s tests -p test_model_catalog.py -v
.venv/bin/python -m unittest discover -s infrastructure/ai-gateway/tests -v
tofu -chdir=infrastructure/ai-gateway init -backend=false
tofu -chdir=infrastructure/ai-gateway fmt -check -recursive
tofu -chdir=infrastructure/ai-gateway validate
tofu -chdir=infrastructure/ai-gateway test
````
</augment_code_snippet>

Use OpenTofu 1.12; confirm every cloud provider is mocked. Extend/run tests when routes, auth, privacy or recovery
changes; mock/unit tests do not prove live provider compatibility or readiness.

Recorded catalog verification: **31 offline guard tests**, the **84-test gateway suite** passing
with **one opt-in admin test skipped**, and **26 mocked OpenTofu tests** passing. The actual
admin-container test passed separately; the skip is not its evidence. The real pinned-image
multi-provider catalog isolation proof also passed. These are recorded results, not claims
that ordinary discovery above runs opted-out container tests; see [deployment status](DEPLOYMENT.md).

### Real catalog isolation proof (local images only)

This executes the unchanged guard in a real, already-local **Python 3.11+ image**, with the
pinned Bifrost and Caddy images also already local. It uses Python HTTP requests in the isolated
container network, not a mock guard or source inspection as runtime evidence. No image or package
downloads, external provider calls or production changes occur. Optional `GATEWAY_MODEL_PYTHON_IMAGE`
selects an existing local image; otherwise the test probes local image IDs and fails if none is usable.

<augment_code_snippet mode="EXCERPT">
````sh
GATEWAY_MODEL_CONTAINER_TESTS=1 .venv/bin/python -m unittest discover \
  -s infrastructure/ai-gateway/tests -p test_model_discovery.py -v
````
</augment_code_snippet>

The disposable fixture creates synthetic providers and keys through supported management APIs,
reads back saved provider-key/model grants and checks exact single-/multi-provider model identities,
cross-key isolation, missing/invalid/revoked/no-grant keys, empty deployments, query/header bypasses
and metadata removal. Only the fixture uses loopback provider catalogs/private-network overrides;
it is removed after the test, leaving production defaults and stored configuration unchanged.
The guard receives no admin/provider credentials; fixture administration is separate.
This proves catalog scoping for the pin, not paid inference, production TLS or GCE/systemd behavior.

### Other opt-in container checks

Opt-in container smoke test (Docker required; pulls pinned images, uses temporary synthetic
credentials and loopback HTTP ports, removes only its own containers/temp files; no external provider calls):

<augment_code_snippet mode="EXCERPT">
````sh
GATEWAY_CONTAINER_TESTS=1 .venv/bin/python -m unittest discover \
  -s infrastructure/ai-gateway/tests -p test_containers.py -v
````
</augment_code_snippet>

This checks startup, private admin authentication, test-only provider creation via the management API,
and negative auth/route boundaries. Its named unavailable-guard scenario intentionally has no listener
on 9092 and verifies catalog 502, not a working catalog. Synthetic providers point at an unreachable
loopback port; the named test-only private-network override must never be copied into production providers.
It does not check GCE provisioning,
IAP, TLS issuance, recovery or positive provider/model routing. Before opening traffic, verify those
on the deployed instance and test client-isolation rules with deliberately scoped virtual keys.

Run the actual admin test separately (also pulls the pinned images; uses the root Python environment's
existing crypto libraries). Unlike the local-only catalog proof, these older smoke tests may download images:

<augment_code_snippet mode="EXCERPT">
````sh
GATEWAY_ADMIN_CONTAINER_TESTS=1 .venv/bin/python -m unittest discover \
  -s infrastructure/ai-gateway/tests -p test_containers.py \
  -k test_pinned_admin_requires_iap_and_bifrost_auth -v
````
</augment_code_snippet>

Upstream contracts: [release](https://github.com/maximhq/bifrost/releases/tag/transports%2Fv2.2.1),
[pinned schema](https://github.com/maximhq/bifrost/blob/transports/v2.2.1/transports/config.schema.json),
[authentication](https://docs.getbifrost.ai/deployment-guides/config-json/authentication),
[source of truth](https://docs.getbifrost.ai/deployment-guides/config-json/source-of-truth).