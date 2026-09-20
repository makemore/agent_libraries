# Observability: Grafana, Prometheus and Node Exporter

Only Grafana is a web application behind root ingress. Prometheus and Node
Exporter remain on `observability-private` (`internal: true`), with no published
ports, `edge` membership, external route, Docker socket or third-party exporter.

## Verified upstream families

Reviewed 2026-09-20 against these official sources:

| Key | Official image and reviewed family | Source |
| --- | --- | --- |
| `images.grafana` | `grafana/grafana`, **12.4.x** | [v12.4.0 Dockerfile](https://github.com/grafana/grafana/blob/v12.4.0/Dockerfile) |
| `images.prometheus` | `prom/prometheus`, **3.x** (Dockerfile v3.5.0) | [Dockerfile](https://github.com/prometheus/prometheus/blob/v3.5.0/Dockerfile), [storage](https://prometheus.io/docs/prometheus/latest/storage/) |
| `images.node_exporter` | `prom/node-exporter`, **1.x** (v1.9.1) | [Dockerfile](https://github.com/prometheus/node_exporter/blob/v1.9.1/Dockerfile), [collector/container documentation](https://github.com/prometheus/node_exporter#docker) |

Use reviewed patch releases and architecture-correct digests, not those tags
verbatim or an assumed latest version. Root requires `@sha256:...` references.
No digest/image runtime has been validated here. Grafana's
[Docker documentation](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/)
says to use `grafana/grafana`; `grafana/grafana-oss` stops receiving updates with
12.4. The template is OSS, not dependent on Enterprise licensing or plugins.

## Rendering, assets and lifecycle

Render `compose.yaml.tftpl` with `{images = map, domains = map}`. Required domain:
`domains.metrics`. Root defines `edge`, where Caddy proxies to **`grafana:3000`**.
Stage the rendered file at `/opt/business-tools/observability/compose.yaml` and
copy these static assets without Terraform interpolation:

- `prometheus.yml` -> `/opt/business-tools/observability/prometheus.yml`.
- `provisioning/datasources/prometheus.yaml` ->
  `/opt/business-tools/observability/provisioning/datasources/prometheus.yaml`.

Static files must be root-owned **`0644`**, not root-only `0600`: the non-root
containers need to read their bind-mounted files. Provisioning/config source
directories may remain root-only on the host because files are bound individually.
Missing sources fail startup (`create_host_path: false`).

Root systemd starts/stops the entire merged project using
`up --abort-on-container-exit`. There are no independent services, Docker restart
policies, host networking or container names. Docker logs rotate at `10m` / `3`.
An unhealthy healthcheck status is not a container exit; root monitoring must
handle that distinction rather than relying solely on Compose's abort flag.

## Runtime secret contract

Root's out-of-band JSON-to-env writer must allowlist these required keys into
`/run/business-tools/grafana.env` unchanged:

| JSON / env key | Requirement |
| --- | --- |
| `GF_SECURITY_ADMIN_USER` | Explicit nonempty initial administrator name |
| `GF_SECURITY_ADMIN_PASSWORD` | Strong nonempty initial administrator password; no default/example |
| `GF_SECURITY_SECRET_KEY` | Stable high-entropy secret for Grafana database secret encryption; generate once and back up securely |

Create the file `root:root`, `0600`, in a `0700` runtime directory, before startup.
Use **Compose >= 2.30** (`env_file.format: raw`): plain `KEY=value` lines without
shell quotes/interpolation; reject CR, LF and NUL. Credentials must never enter
Terraform variables/state, rendered templates, command arguments or logs.
Do not print a resolved Compose configuration containing these env values.

Anonymous access and self-signup are explicitly disabled. SQLite in the data
bind holds users/dashboard state; no external Grafana DB credentials are needed.
The admin environment is initial provisioning, **not password rotation** for an
existing database. Rotate through supported administration and reconcile the
secret store deliberately. Keep the encryption key stable across restarts and
restores; merely replacing it can make stored credentials unreadable.

Optional SMTP can be added to the same file, explicitly allowlisted by root:
`GF_SMTP_ENABLED`, `GF_SMTP_HOST` (host:port), `GF_SMTP_USER`, `GF_SMTP_PASSWORD`,
`GF_SMTP_FROM_ADDRESS`, `GF_SMTP_FROM_NAME`, `GF_SMTP_STARTTLS_POLICY`. Do not turn
off TLS verification. SMTP is not needed for login or querying local metrics;
email notifications need transport and alert rules configured separately. See
[Grafana configuration](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/).

## Persistence and ownership

| Host path | Container path | Owner/mode created by root |
| --- | --- | --- |
| `/srv/business-tools/observability/grafana` | `/var/lib/grafana` | `472:0`, `0700` |
| `/srv/business-tools/observability/prometheus` | `/prometheus` | `65534:65534`, `0700` |

Grafana's Dockerfile defines UID 472 / GID 0 and its data/plugin paths; this
template pins that numeric identity explicitly. Prometheus and Node Exporter
images declare `USER nobody`; this deployment explicitly uses **65534:65534**
to give root a deterministic bind-ownership contract, not an assumption that all
future image `/etc/passwd` files are identical. Node Exporter needs no data dir.
Create binds after mounting the persistent disk; preserve existing data/choices.
Do not rerun a seed or reset a Grafana database to fix permissions.

Prometheus retains **15 days or 5 GB of blocks, whichever limit is reached first**.
Retention is not a hard filesystem quota: WAL/head and compaction need additional
space and deletion is asynchronous. Reserve at least 6–8 GiB for this small TSDB,
monitor capacity, and use a local POSIX filesystem (not NFS). Neither this nor
Grafana is HA; disk/VM failure can lose data. Back up SQLite while stopped through
root's owner, or use a validated consistent backup; test restoration privately.

## Host visibility and limitations

Node Exporter gets only **`/proc:/host/proc:ro` and `/sys:/host/sys:ro`**, with all
capabilities dropped, no privileged mode, and no shared host PID/network namespace.
The enabled collector allowlist is cpu, cpufreq, diskstats, loadavg, meminfo,
pressure, stat, time and vmstat. It reports host CPU/memory/load/disk I/O from those
mounts; pressure/cpufreq depend on kernel/VM availability. Restrictive host procfs
permissions may limit individual metrics; do not broaden privileges silently.

Unlike upstream's full-host example, this deliberately does **not mount `/`**:
there are no filesystem-capacity metrics, private host data mounts or systemd
socket. Network collectors are disabled because `/proc/net` and `/sys/class/net`
are namespace-sensitive: under bridge networking those counters would describe
the **container network, not host interfaces**. There are no per-container/app
metrics, costs, HTTP uptime probes, logs, traces or off-VM failure alerts. Do not
interpret Prometheus target `up` as proof every business application is healthy.

## Provisioning and setup validation

[Supported provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
creates datasource UID `business-tools-prometheus`, URL **`http://prometheus:9090`**,
server/proxy access and a 15-second query interval. Only this datasource is
managed; existing unrelated datasources/dashboards are not deleted. No third-party
dashboard/plugin is downloaded, and operator-created dashboards remain in SQLite.

1. Render/merge with root and run `docker compose config --quiet` using the full
   file list. Use an isolated VM for subsequent checks, not the host's data binds.
2. Validate the Prometheus config with `promtool check config` before launch, or
   `docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.yml`
   in the root-owned test stack. Require successful exit and no config errors.
3. Check Grafana `/api/health`, Prometheus `/-/ready` and Node Exporter `/metrics`
   internally. From Grafana Explore, confirm `up{job="prometheus"}` and
   `up{job="node"}` both equal 1; check collector success for supported collectors.
4. Keep **root `public_enabled = false`**, blocking ALL public app paths. Initialize
   via root's private loopback/IAP route using canonical HTTPS (secure cookies
   remain enabled). Verify admin login, no anonymous dashboard access, and no
   self-signup. Root alone decides when to open public Grafana, never Prometheus.
5. Useful initial dashboard queries: `node_load1`, `node_memory_MemAvailable_bytes`,
   `100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))`, and
   `rate(node_disk_read_bytes_total[5m])`. Save a dashboard in the UI and verify it
   and metric history survive a root-systemd restart. No dashboard is preseeded.
6. Test closed/public ingress separately from container health. Check retention
   configuration and available disk; no host network or filesystem-capacity
   metric is promised by this minimal mount policy.

Authoring validation was source/static review only. No command-execution tool was
available, so Terraform/Compose rendering, promtool and runtime tests remain to
be executed. No deployment, dependency install or image pull was performed.