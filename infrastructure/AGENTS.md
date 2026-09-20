# Infrastructure operating rules

Scope: all of `infrastructure/`. Follow the repository's root guidance too; consult
the owning module's guide before changing or operating it. Documentation and example
configuration are **not evidence of deployment, readiness or an authenticated connection**.

## Ownership and map

| Path | Responsibility and authoritative guide |
| --- | --- |
| `ai-gateway/` | Root-repository-owned Bifrost gateway, separate VM/network/IAM/data and `ai-gateway/state`. Start with [README](ai-gateway/README.md), [deployment record](ai-gateway/DEPLOYMENT.md) and [optional browser IAP](ai-gateway/ADMIN.md). Do not alter Studio resources or project API lifecycle. |
| `business-tools/` | Root-owned shared VM, independent `business-tools/state`, one merged Compose/systemd lifecycle. [Operator guide](business-tools/README.md) owns deployment, secrets, ingress and recovery. |
| `raise-crm/` | Separate application code: [Django backend](raise-crm/backend/readme.md), [profile-scoped local MCP](raise-crm/backend/docs/MCP.md) and [Next.js frontend](raise-crm/frontend/README.md). Not a business-tools Compose app; frontend starter docs do not prove CRM integration/deployment. |
| `machine/` | Git submodule for `mach` development-machine provisioning. Read its [README](machine/README.md) and `docs/`; respect its separate repository and provider lifecycle. Do not replace Terraform roots with Machinefiles implicitly. |
| `agentic-social/` | [README redirect only](agentic-social/README.md); obsolete standalone deployment sources were removed. Postiz belongs under `business-tools/agentic-social/`. Source cleanup does not migrate or delete cloud resources, data, DNS or remote state. |
| `pipeboard/` | [Selected paid hosted ads management](pipeboard/README.md), with human account/payment/OAuth steps. No local server, Terraform root or sixth business-tools VM service. |

The shared VM has **five application groups**: Postiz (`agentic-social/`), Mautic
(`mautic/`), Actual Budget (`actual-budget/`), Invoice Ninja (`invoice-ninja/`), and
Grafana/Prometheus (`observability/`). Their app READMEs own integration contracts;
do not start fragments as separate Compose projects. Postiz publishing is distinct
from Pipeboard paid-ad management. Raise CRM and the AI gateway remain separate.

## Sources, defaults and approval boundaries

- Read public READMEs, `.tf` sources, `.example` files, explicit runtime/templates
  and tests. Do not inspect local `backend.hcl`, `terraform.tfvars`, saved plans,
  state, env files, credentials or backend-local configuration as documentation.
  Review sensitive operational artifacts only through an approved private workflow.
- Omit optional settings to inherit supported defaults. Preserve existing overrides,
  dashboard choices and data. Never weaken storage, ACLs, retention or validation to
  pass a demo. Copy examples only when destination files are absent.
- Each Terraform/OpenTofu root owns isolated state and resources. Existing projects,
  enabled APIs and access-reviewed GCS state buckets are prerequisites, not resources
  to silently create or share. Verify image digests and compatibility; placeholder
  image values and synthetic mock hashes are not deployable release pins.
- Source edits and isolated offline checks do not authorize live operations. Require
  **explicit targeted approval** for cloud changes/apply, DNS/IAM changes, public
  activation, restarts, migrations, restore, account creation/purchase, subscriptions,
  provider connection or spend. Identify stack/project/state, exact targets, cost,
  downtime and recovery plan. An operator manually applies the reviewed saved plan;
  never auto-approve, destroy safeguards or interpret “finish setup” as blanket consent.
- New dependencies/version changes, including the optional Pipeboard CLI, require
  user confirmation. Use package managers after approval; do not silently install
  tools or modify global client configuration.

## Security and private initialization

Both GCE roots use IAP-only SSH/OS Login, resource-scoped VM identities, retained
data disks and deletion protections. Optional operator grants default empty and
DNS management defaults off. Keep these boundaries; do not expose admin/database
ports for troubleshooting. Gateway browser IAP is a separate explicit opt-in and
does not replace Bifrost authentication or per-client virtual-key restrictions.

Terraform creates bootstrap secret **containers, not secret versions**. Supply
versions out of band using protected files/stdin or an approved secret manager,
never literals in argv, URLs, metadata, state, source or chat. Runtime preparation
fails closed until valid secrets exist; systemd retries. `/run/<stack>` contains
root-only runtime credentials; `/opt/<stack>` holds staged sources and
`/srv/<stack>` retained data. Preserve database/encryption-key pairings; changing
bootstrap values does not rotate existing users or reset administrator accounts.

Business-tools defaults to `public_enabled = false`: Caddy/ACME web ports remain
open but public application paths are gated. Initialize all five apps privately
via IAP with canonical HTTPS origins, valid TLS and secure cookies. Follow the
[root activation procedure](business-tools/README.md), [Mautic proxy-trust guide](business-tools/mautic/README.md)
and [Invoice canonical-access/PDF guide](business-tools/invoice-ninja/README.md).
Do not substitute localhost URLs, wildcard proxy trust or hosted PDF rendering.
SMTP, social OAuth, sending and ad spend are separate approvals, not deployment effects.

The first-install Mautic seed trusts only Caddy at `172.30.251.2` on `mautic-proxy`;
changed/existing unrecognized configuration is preserved and refused, not repaired.
Invoice's closed-gate HTTPS exception matches only transport peer `172.30.252.3`
on `invoice-render`, never forwarded headers. Keep these two-peer `/29` networks,
aliases, seed and Caddy matcher coordinated; check subnet collisions before launch.
Neither configuration contract proves selected-image HTTPS/PDF runtime behavior.

Never dump environments, resolved Compose configuration, `docker inspect`, secret
files, OAuth callbacks or raw logs/API responses. Financial data and backups are
sensitive too. Report only approved identifiers, counts, states and exit codes.

## Shared lifecycle and recovery

`business-tools.service` owns the foreground merged Compose project. One container
exit stops the whole project (`--abort-on-container-exit`); unhealthy-but-running
containers do not trigger that behavior. The wrapper clears profile overrides;
Mautic async workers require reviewed configuration, not a shell profile export.

An applied startup-metadata change does **not** execute/stage it. After backup and
maintenance approval, rerun the GCE metadata startup script per the owning guide
(or explicitly approve a reboot); rerunning only an old staged bootstrap is not an
update. Preserve existing data/settings. Maintenance holds the maintenance lock,
stops systemd before taking the exclusive data lock, and releases that lock before
restart. Do not bypass disk/mount checks, format data or recursively reset ownership.

Business-tools cold backups stop the **whole stack** daily at 03:00 UTC plus jitter;
shutdown alone can exceed an hour. Archives include data and runtime credentials,
share the source disk and are pruned by age after 14 days. No off-host copy is
implemented. Separate 04:00 snapshots are **crash-consistent**, do not wait for
backups, and are not proof of restore. Gateway online SQLite backups have a different
contract; use its guide. Test restores on isolated targets with delivery/public
ingress controlled; never extract over live data or assume image rollback reverses
a schema migration. Plan independent monitoring because shared observability fails
with the shared VM.

## Validation and evidence

Use the root `.venv/bin/python`; Django/pytest must use isolated test settings,
never the host database. From repository root, the shared-stack checks are:

<augment_code_snippet mode="EXCERPT">
````sh
.venv/bin/python -m unittest discover -s infrastructure/business-tools/tests -v
.venv/bin/python -m unittest discover -s infrastructure/business-tools/agentic-social/tests -v
.venv/bin/python -m unittest discover -s infrastructure/business-tools/mautic/tests -v
````
</augment_code_snippet>

Follow [gateway validation](ai-gateway/README.md#local-verification-no-deployment)
for its separate suites. Use OpenTofu 1.12 for mock tests (business-tools documents
1.12.6). Business-tools `tests/deployment.tftest.hcl` currently defines **19
plan-only mock runs**, not live-cloud tests. Run native tests in a fresh disposable
copy of reviewed module source, excluding local tfvars/backend/state/plans/secrets:
`tofu init -backend=false`, `tofu fmt -check -recursive`, `tofu validate`, then
`tofu test -filter=tests/deployment.tftest.hcl`. Confirm every provider is mocked;
provider installation may still need registry access. Do not use mock apply.

Update relevant default-path and named-exception tests with runtime/security
changes; run the smallest suite first and record passes **and skips**. Offline
checks do not prove selected-image compatibility, live TLS/IAP/OAuth, delivery,
restoration or connection status. Those require separately approved runtime checks.