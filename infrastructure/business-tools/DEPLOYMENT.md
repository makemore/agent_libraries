# Shared business-tools deployment record

## Metadata applied; staging awaits CLI reauthentication — 2026-09-21

The user approved repairing shared shutdown and persistent Mautic session security,
including controlled downtime. Public access, image/package versions, credentials,
provider connections and delivery settings remain unchanged.

### Shutdown and cold-backup evidence

- The initial `stop → monitor wait → down` repair was installed with protected
  config copies. Its backup retry at **00:24:52–00:26:56 UTC** still failed closed:
  systemd recorded foreground exit **143**, and cleanup restarted the stack.
- A subsequent isolated signal-based candidate returned **130** on the exact
  Compose version and was rejected, never installed. Neither code was accepted
  as success; the backup guard and unit success policy were not weakened.
- The final helper stops **Caddy first**, then the whole project, waits for all
  original foreground monitor pidfds, and only then removes containers. This
  preserves Compose's first-observed-exit semantics, not an all-container-exit
  success guarantee. Caddy closure also interrupts its internal Invoice asset
  route; this is whole-stack maintenance, not proof of lossless worker draining.
- Exact-VM, isolated Compose **2.40.3+ds1-0ubuntu1~24.04.1** rehearsal: **6/6 pass**.
  Two native-Caddy/downstream-143 stops and a both-zero control returned success/0.
  Caddy failure, an earlier downstream failure during ingress drain, and a
  spontaneous downstream failure all retained exit **7**. The spontaneous case
  had two stopped containers before explicit fixture cleanup, not an automatic
  ExecStop/removal claim. Every exact fixture was removed afterwards.
- The revised helper was installed by hash-guarded config-only handoff, without
  changing the running MainPID or unit bytes. Protected previous config/helper
  copies are at `/var/tmp/business-tools-stop-repair-6ef5vjyw` (not a data backup).
- **Cold backup succeeded**, exit **0**, at **01:09:24–01:11:59 UTC**. The strict
  clean-stop and no-project-containers checks passed; cleanup restarted the stack.
  Archive `backup-20260921T011128517384535Z.tar.gz` is **117,847,567 bytes**, root-owned
  **0600**, passes a separate gzip integrity check, and has no incomplete sibling.
  Free space remains **138 GiB**. This is a same-disk archive, **not** a restore
  drill or off-host disaster-recovery copy.
- After startup settled, **17/17 containers are running**, all configured
  healthchecks pass, systemd is active/success with **0 automatic restarts**, and
  Invoice's private canonical HTTPS root returns **200** again. The old Postiz
  nginx-only healthcheck still misses its API failure; no API readiness is claimed.

### Mautic regression and reviewed deployment plan

The selected-image cache regression now passes: supported fresh CLI installation,
two web/cron recreations, unchanged administrator/canonical URL/proxy seed, fresh
private tmpfs caches, and **Secure + HttpOnly** login cookies. The exact disposable
project and synthetic volumes were removed. The named fixture simulates TLS
termination through the already-trusted proxy peer; it does **not** prove live TLS.
The CLI-only port-default compatibility shim and transport simulation are scoped
and tested in `scripts/check-mautic-cache.py` and documented in the Mautic guide.

**The Mautic and Postiz runtime changes are not staged.** At the last successful
private check after backup restart, Mautic still issued HttpOnly cookies without
Secure and Postiz's API returned 502. No Temporal sample attribute was removed
and no Postiz administrator created.
All five external HTTPS roots still return **503**, with certificate verification.

`business-tools-maintenance-fix.tfplan` is a protected, reviewed **0-add / 1-update /
0-destroy** plan for `google_compute_instance.business_tools` in the same project
and `business-tools/state`. Only startup-script metadata changes: the two app
Compose assets, `stop.py`, its service, bootstrap and disk-check staging lists.
The public gate and all image pins, machine/disks, network, DNS and IAM are unchanged.
Plan SHA-256: `5377bbec9dadcee2ac4a3ab62fde7b67c96f22da27f4120f6da25291d206abd5`.
**Applied successfully**, exit **0**, after the user explicitly approved this
exact plan as a new one-time agent-apply exception and authorized the controlled
restart/remaining setup. The plan hash, project/VM/zone, closed public gate and
single metadata-only update were rechecked immediately before applying. OpenTofu
reported **0 added, 1 changed, 0 destroyed**. Do not apply either saved plan again.

The following IAP SSH attempt failed locally because the **gcloud CLI requires
interactive reauthentication**. A read-only SSH probe and VM-description request
confirmed the same authentication blocker. No remote startup command executed;
there was no new controlled outage, attribute removal or account creation.
OpenTofu's successful apply does not prove that the updated metadata was staged.
The operator must refresh the existing deployment account with `gcloud auth login`
in a private interactive terminal, without sharing credentials or authorization
codes. Then compare live metadata with this applied plan, verify the recovery
archive, rerun metadata startup, complete the approved Temporal cleanup/account
setup, and verify canonical-HTTPS session security and registration restrictions.
Do not reboot as a workaround or rerun only the old staged bootstrap.

Validation: **351 Python tests passed, no skips** (333 root + 9 Postiz + 9 Mautic).
Fresh backend-disabled OpenTofu init, formatting, validation and **19 mocked plans**
passed; authenticated saved-plan creation and whitespace checks exited 0.
Keep the regression tests updated and rerun them, the exact-version lifecycle
rehearsal and cache check after relevant changes. Full configured-app restore,
Invoice PDF, MFA, independent monitoring and public activation remain separate gates.

## Earlier infrastructure and initialization record — 2026-09-20

User approved the shared host, local databases, image downloads and isolated tests.
Crimson's database is not used or changed. AI gateway and Raise CRM remain separate;
Pipeboard remains hosted. After reviewing the create-only plan, the user explicitly
authorized a **one-time exception** to the operator-only apply rule. The agent
applied that exact saved plan: **12 added, 0 changed, 0 destroyed**, exit 0.
This exception does not authorize future applies, public activation or provider sends.

| Setting | Reviewed value |
| --- | --- |
| Project | `makemoredigital2025` |
| State | `gs://makemoredigital2025-tfstate`, prefix `business-tools/state` |
| Instance / zone | `business-tools` / `europe-west2-b` |
| Reserved external IPv4 | `34.142.7.165`, verified in use by this VM only |
| Machine | `e2-standard-4`, 4 vCPU / 16 GB |
| Disks | 50 GB boot, 150 GB retained balanced application disk |
| Public app gate | Closed (`public_enabled = false`) |
| DNS / operator IAM | No DNS changes; no optional operator grants |
| Budget | Approved planning estimate US$160–200/month; not a spending ceiling |

Backend bucket metadata confirms uniform access, enforced public-access prevention
and versioning. Existing deployer credentials were refreshed before planning.
Local `backend.hcl`, `terraform.tfvars`, backend artifacts and the 0600 saved plan
remain Git-ignored; no secret values are in Terraform variables.

The applied `business-tools.tfplan` contained **12 creates, 0 updates, 0 deletes**:
VM, retained data disk, static IPv4, VPC, subnet, web firewall, IAP SSH firewall,
snapshot schedule, disk/schedule attachment, service account, empty bootstrap
secret container and that identity's secret-access grant. There are no resources
targeting another stack. The boot disk is part of the VM resource.

Post-apply metadata verifies the VM is `RUNNING` with deletion protection and
Shielded VM protections enabled. The 150 GB data disk is `READY`, attached without
auto-delete, and has the daily 04:00 UTC snapshot policy with 14-day retention.
Firewalls permit only public TCP 80/443 and IAP-sourced TCP 22 for this VM identity.
A refreshed OpenTofu plan exited **0: no changes**, confirming no observed drift.

The bootstrap secret now has an enabled version. Distinct generated credentials
are in operator-owned recovery JSON files outside the repository (0700 directory,
0600 files); values were never printed or passed in arguments. Do not regenerate.
IAP SSH works; data is mounted, `business-tools.service` is active with zero
automatic restarts, and the backup timer is active/waiting. All 17 containers run.
Configured healthchecks pass, but **Postiz's deployed nginx-only check misses a
failed API**. The first manual execution of the cold-backup service failed closed
during shutdown; no archive was published. Its cleanup restarted the stack.
A snapshot policy is not a completed backup. See maintenance findings below.

### Applied bootstrap correction

Ubuntu's installed Compose reports `2.40.3+ds1-0ubuntu1~24.04.1`; the existing
version check incorrectly rejects the numeric distro suffix. Local source now
accepts that suffix only after build metadata, while still rejecting upstream
prereleases and versions below 2.30. No package or version change is proposed.
Regression tests reproduced the defect, then all **135 Python tests passed** after
the narrow fix. Keep this packaging case in tests when changing the version gate.

User explicitly approved the protected `business-tools-bootstrap-fix.tfplan`:
**0 adds, 1 in-place update, 0 destroys**, changing only the VM's startup-script
metadata. Public access remains closed; no VM replacement, IAM or DNS changes.
The exact reviewed plan was **applied successfully**, then the GCE metadata
startup runner was executed via IAP, also exit 0. It staged the updated assets,
mounted/prepared storage and started the merged project. No app containers existed
before this initialization. No public activation or unrelated stack changes.

### Private account and security evidence

User authorized `chris.barry@makemoredigital.com` as administrator where supported.
Canonical HTTPS certificate validation succeeds on all five hosts, through the
loopback-only IAP route and externally. External `/`, `/installer`,
`/api/v1/clients` and `/uploads/security-check` all return **503** on all five hosts.
Only Caddy publishes host ports; its private listener is `127.0.0.1:8443`.
Container-to-metadata DROP rules are present for both Docker bridge patterns.

| App | Verified state |
| --- | --- |
| Mautic | Username `chris.barry`, requested email, one user with administrator role; successful login/account read and role-administration page. Canonical site URL persisted; installer redirects away. **Restart regression:** fresh sessions remain HttpOnly but no longer have Secure; public activation is blocked. |
| Invoice Ninja | Requested email authenticates as owner **and** administrator. Unauthenticated client API denied; `/setup` redirects to the canonical HTTPS root. |
| Grafana | Requested email is both login and saved profile email; server-admin role verified. Anonymous access/self-signup disabled, secure cookies configured, provisioned Prometheus datasource healthy; `node` and `prometheus` scrape targets both report up=1. |
| Actual | Distinct server password set via supported bootstrap endpoint; password login yields ADMIN. No email-based account in this mode. Anonymous file listing and repeated password-bootstrap rejected. |
| Postiz | **Not initialized**: API returns 502; frontend/container health alone is insufficient. See blocker below. |

Mautic's database was independently verified empty immediately before installation,
with exact proxy seed and recognized upstream first-run configuration. The web
installer created the account, but its final step redirected instead of completing:
migration bookkeeping was absent and cached session cookies lacked Secure.
No reinstall/reset was performed. As part of the authorized fresh initialization,
ran the supported absolute-path console as `33:33`: `cache:clear --no-warmup`,
`doctrine:migrations:sync-metadata-storage`, then the installer's own
`doctrine:migrations:version --add --all`. All completed with exit 0; status reports
**62 executed / 62 available / 0 new / 0 unavailable**. Fresh login now verifies
Secure+HttpOnly cookies at that point. **This did not survive the later backup
stop/restart:** fresh cookies again lack Secure. The cache clear is not a durable
repair; do not repeat it as evidence of readiness. No cookie-policy override or
proxy widening was applied.
The image's CLI working directory is not `/var/www/html`; use the absolute console
path. Its generated `local.php` is 0755 inside the protected 0700 config directory;
the separate proxy seed remains exactly 0600. No ownership/mode reset was performed.

MFA is **not enrolled**; the operator must register an authenticator where supported.
No SMTP/provider connections or test sends were made. Browser UX, Invoice PDF,
durable restart security, full backup/recovery and independent monitoring remain gates.

Reusable private account helpers and **47 new synthetic tests** were added. Final
sweep: **182 Python tests passed, no skips** (164 root, nine Postiz, nine Mautic),
all exit 0; whitespace check passed. Repeated live login/security checks for the
four initialized apps also exited 0 without recreating accounts or resetting
passwords. This does not cover the blocked Postiz setup or the remaining launch
gates. Keep the tests updated and rerun them when changing the setup flows.

### Postiz correction approved; production change paused for recovery

Guest diagnostics match [upstream issue #1504](https://github.com/gitroomhq/postiz-app/issues/1504):
Temporal SQL visibility permits three Text search attributes. Auto-setup created
`CustomTextField` and `CustomStringField`; Postiz needs two more (`organizationId`,
`postId`), so its backend fails. Both sample fields are present, both Postiz fields
absent, and workflow count is zero. No sample field was removed.

The user explicitly approved the configuration update, removal of these two unused
sample fields and controlled shared-stack restart. Source now sets the pinned
Temporal entrypoint's supported `SKIP_ADD_CUSTOM_SEARCH_ATTRIBUTES=true` and adds
a bounded frontend **and API** healthcheck. This skips future demo creation only;
existing attributes are not automatically removed. Namespace, databases, workflow
data, other attributes, image pins, limits and public gate remain unchanged.

The protected `business-tools-postiz-fix.tfplan` was reviewed: **0 adds, 1 in-place
update, 0 destroys**. Only the Postiz Compose asset in VM startup-script metadata
changes; every other staged asset and infrastructure setting is unchanged.
**Not applied. No Temporal field has been removed, and no Postiz account created.**

- **14 new health/configuration regressions pass**, including frontend success
  with API 502, both registration booleans, invalid/truncated/oversized responses,
  refusal and bounded socket/overall timeouts.
- The selected-image isolated Postiz smoke **passed**, exit 0, with all five
  containers healthy under the new backend-aware probe. Its exact synthetic
  project, volumes and networks were cleaned up; synthetic data is not recoverable.
- Added a separately invoked second-signup verifier and **10 offline tests**.
  It requires a closed policy and an unauthenticated private client, generates
  a distinct synthetic address, and expects tagged v2.23.0's exact HTTP 400
  plain-text `Registration is disabled` response. Duplicate-email/validation
  errors do not count. This verifier has **not run against production**.
- Final Python sweep: **206 tests passed, no skips** (188 root, nine Postiz,
  nine Mautic), all exit 0. Whitespace check passed. Keep these tests updated and
  rerun them alongside real-image/lifecycle checks when changing the repair.

### Cold-backup attempt and restart security findings

Before any corrective apply/removal, rechecked zero Temporal workflows, both
sample Text fields present, both Postiz fields absent and 138 GiB free. Started
the existing `business-tools-backup.service` under the approved maintenance scope
at **23:02:59 UTC**. At **23:05:02 UTC** it finished with status 1; no completed
or incomplete archive remains. The stack was restarted by its cleanup path.

The application unit's foreground process exited **2** during stop; its result was
`exit-code`, with no recorded control-process failure. The backup requires a clean
stop and therefore correctly refused to publish an archive. `prepare-disk.py
--check-backup` subsequently passes, and capacity remains 138 GiB. Do not reset
failure state, accept exit 2 as success, bypass the stop guard or delete fields
without recovery preparation. The backup timer remains enabled; its latest service
result is failed, not evidence of a successful scheduled backup.

Source review found a matching upstream foreground-monitor/removal race:
[Compose #13559](https://github.com/docker/compose/issues/13559) and
[fix #13551](https://github.com/docker/compose/pull/13551). The deployed Ubuntu
2.40.3 package contains the affected code, but no retained panic trace confirms
this incident's cause; an application shutdown exit is another possibility.
No Compose version change, success-code override or lifecycle repair was applied.
Reproduce the foreground `up`/`down` lifecycle in isolation before selecting a fix;
the existing detached startup smoke does not cover this race.

After cleanup, all **17 containers are running**, configured healthchecks pass,
and systemd reports active with zero automatic restarts. Postiz's actual API still
returns **502**. Actual, Grafana and Invoice Ninja repeated login/security checks
pass. Mautic's installer remains locked and login/account/role-page reads succeed,
but fresh session cookies are **HttpOnly without Secure**. The combined four-app
verification correctly exited 1; subsequent targeted diagnosis confirmed the
Mautic cookie regression. No cache clear, account reset or security override was
performed to hide it.

All five external HTTPS roots still return **503**, with certificate validation;
the temporary private tunnel was closed. Expand approval to a tested shared-shutdown
repair and durable Mautic HTTPS-session repair before another live maintenance
attempt, then complete recovery preparation and the already-approved Postiz fix.
Public activation, SMTP/providers and full configured-app restore remain separate
gates; this attempt produced no usable recovery archive.

## Release and validation evidence

Explicitly selected [image-only manifest](releases/2026-09-20.tfvars.json):
Postiz 2.23.0, PostgreSQL 17 Bookworm, Temporal 1.28.1 with separate PostgreSQL 16
Bookworm, Redis 7.2 Bookworm, Mautic 6.0.9-20260603 Apache, MariaDB 11.4,
Actual Budget 26.9.0 **Debian**, Invoice Ninja Debian 5.13.43, Nginx 1.30 Alpine,
Grafana 12.4.9, Prometheus 3.14.0, Node Exporter 1.12.1 and Caddy 2.10.2.
Each reference includes its verified registry digest. All 14 linux/amd64 pulls
succeeded. Tags describe releases; the digest, not the tag, selects the image.

- All **five application groups** passed the real-template startup harness,
  including PostgreSQL/Redis/Temporal dependencies, Mautic cron and Invoice PHP-FPM/Nginx.
  Healthchecked services reached healthy; Mautic cron was running (no healthcheck).
- Cold restore checks passed for **PostgreSQL 17, PostgreSQL 16 and MariaDB 11.4**:
  insert synthetic row, clean shutdown/exit 0, GNU tar into fresh volumes, restart
  from restored data, query exact row. The transfer helper needed 512 MiB rather
  than the directory initializer's 64 MiB after a MariaDB transfer exited 137.
  This is test-only; production host backup/resource settings were not changed.
- Native real tar/gzip roundtrip passed for synthetic database files, media, config,
  all nine runtime-key files, permissions, fixture ownership, symlink and readable
  SQLite data. Backup recursion was excluded. Production ACL/xattr variants and
  full installed-application recovery are not claimed.
- OpenTofu 1.12.6 formatting, validation and **19 mocked plans** passed in a fresh
  backend-disabled source copy. Authenticated cloud init/validate/plan exited 0.
- Final unit sweep: **135 Python tests passed, no skips** (117 root tests, nine
  Postiz tests and nine Mautic tests); whitespace checks passed. All exited 0.
- Test containers and synthetic volumes were cleaned up. No existing data, cloud
  database, provider account, social post, invoice delivery or campaign was changed.

These are bounded local tests on Docker Desktop with amd64 emulation, not a full
merged-systemd rehearsal, performance guarantee or proof of public readiness.
Maintain and rerun the unit suites in the [operator guide](README.md#local-validation-versus-launch-evidence)
alongside the opt-in scripts when relevant code or pins change.

## Applied plan audit

The deployment is now billable. The apply created no secret version; application
startup fails closed until the bootstrap payload is supplied. The saved plan is an
audit artifact, not an instruction to apply again. Inspect only in a private local
terminal; do not paste raw operational artifacts into chat:

<augment_code_snippet mode="EXCERPT">
````sh
cd infrastructure/business-tools
tofu show business-tools.tfplan
tofu output -raw static_ip
````
</augment_code_snippet>

Future changes require a new reviewed plan and targeted approval under the normal
operator-apply rule. Do not silently replace or reuse the applied plan. Pass
`-var-file=releases/2026-09-20.tfvars.json` when planning this release.

**DNS is configured.** These A records point at **34.142.7.165**:

| Name | Application |
| --- | --- |
| `social` | Postiz |
| `marketing` | Mautic |
| `budget` | Actual Budget |
| `invoices` | Invoice Ninja |
| `metrics` | Grafana |

The user reports adding all five records. Both authoritative nameservers
(`ns11.domaincontrol.com`, `ns12.domaincontrol.com`) and Cloudflare's recursive
resolver confirm all five A records are **34.142.7.165**. Google confirms four;
`social` still returns no A answer from that resolver at this check.
No AAAA answers were seen. No DNS writes were performed by the agent.
Prometheus and database services remain private; DNS correctness is not proof
of working HTTPS or app readiness.

## After apply; before public activation

1. Verify VM/disk/snapshot/IAM state, bootstrap prerequisites and subnet collision
   checks, using safe metadata. Provision the new bootstrap secret version through
   the protected-file workflow in [README](README.md#bootstrap-credentials-out-of-band-only).
2. Configure DNS, validate real certificates and the IAP-only canonical-origin
   setup route. Confirm all public app paths remain closed.
3. Initialize all five apps and verify login, signup/install restrictions, persisted
   settings, media, cron/workers, Mautic HTTPS handling and Invoice PDF rendering.
4. Verify the actual cold backup/restart and an isolated configured-app restore,
   plus disk capacity, backup result and independent external monitoring. Daily
   03:00 UTC cold archives share the source disk and can cause lengthy whole-stack
   downtime; separate 04:00 snapshots are crash-consistent. Both retain 14 days;
   snapshots do not wait for archive completion. Keep secured recovery keys.
5. Obtain separate approval for public activation and any SMTP/OAuth/delivery.
   Nothing here authorizes sending messages, social posts, invoices or ad spend.