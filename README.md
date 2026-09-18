# studio — thin REST CLI

Standalone Python **3.11+**, with **no runtime dependencies or Django imports**.
The REST server owns authorization, validation, defaults and persistence. The
CLI supplies connection profiles, credential input, HTTP, pagination and output.
This first slice only inspects metadata. It changes no server APIs or host policy.

## Install / run

This is local source, version **0.1.0**, not a published PyPI release. From the
meta-repository root, install in an isolated tool environment:

<augment_code_snippet mode="EXCERPT">
````sh
uv tool install ./clients/agent-cli
studio --help
````
</augment_code_snippet>

The executable is **`studio`** (formerly `agentctl`). The distribution/import
package remains `agentctl`; existing profile locations and `AGENTCTL_*`
environment variables are unchanged. Command groups are unchanged too, so Studio
workspace commands begin with `studio studio`. No legacy executable alias is
installed. If already installed, reinstall with
`uv tool install --reinstall ./clients/agent-cli` to refresh the entrypoint.

Or run the unchanged Python module directly, without installing anything:

<augment_code_snippet mode="EXCERPT">
````sh
PYTHONPATH=clients/agent-cli/src .venv/bin/python -m agentctl --help
````
</augment_code_snippet>

## Connection setup

An origin is just the scheme, hostname and optional port. API mounts are explicit
same-origin paths, with a trailing slash. Omit integrations not installed there.
**The Studio mount is its API root (`/studio/api/`), not its UI root.**

<augment_code_snippet mode="EXCERPT">
````sh
studio profiles add production --origin https://studio.makemoredigital.com \
  --runtime-mount /api/agent-runtime/ --studio-mount /studio/api/ \
  --identity-path /api/accounts/profile/
studio profiles list
studio profiles use production
````
</augment_code_snippet>

This only creates **local metadata**; it does not contact the host or log in.
First profile is selected; adding others does not change the selection. Existing
profiles cannot be overwritten. Use a new name when changing an installation.
`--profile NAME` overrides selection for one command. There are no ambient origin
overrides or auto-loaded repository config files.

### Credentials and host policy

Use an **already provisioned, host-approved credential**. The CLI does not issue
tokens, accept account passwords, borrow browser sessions or bypass MFA. The
host operator must approve token issuance/revocation and the principal's rights.
Stock DRF tokens are not automatically read-only, scoped or short-lived.

Credential sources, in order (no fallback if the chosen source is invalid):

1. Explicit `--token-stdin`, for a pipe from your approved secret provider.
2. Profile `--credential-file /absolute/private/file`, externally provisioned.
3. `AGENTCTL_TOKEN`, injected into the process by your secret provider.

**Never paste credentials into command arguments, URLs, shell history or chat.**
There is intentionally no `--token VALUE`, credential display, password login,
or credential-writing command. Stdin/files accept one terminal LF/CRLF, but not
embedded whitespace; the credential limit is 8192 ASCII characters. Stdin reads
until EOF: use a pipe, not interactive typing. `Token` is the default DRF scheme;
select `--auth-scheme Bearer` only when the host explicitly supports it.

Config uses `~/Library/Application Support/agentctl` on macOS, or the XDG config
directory on Linux. Override with `--config-dir` or absolute `AGENTCTL_CONFIG_DIR`.
Profiles are atomic 0600 files in a 0700 directory. External credential files must
be owned regular files, 0400/0600, without symlinks or extra hardlinks. Credential
paths/values are omitted from profile listings. Missing/invalid credential files
do not prevent selecting another profile. File-security checks require POSIX;
Windows ACL/keychain support is not implemented. Credentials are never revoked by
removing local configuration; use the host's supported revocation procedure.

## Commands available now

Assuming your credential source is available:

<augment_code_snippet mode="EXCERPT">
````sh
studio status --json
studio whoami
studio runtime agents list --json
studio runtime agents get agent-slug
studio runtime runs list --agent-key agent-slug --json
studio studio projects list
studio studio threads list --project-id PROJECT_ID --all --json
````
</augment_code_snippet>

`studio studio projects get ID` and `studio studio threads get ID` are also supported. Global
flags can appear before or after subcommands. Every command has `--help`.

- **Metadata only:** no prompts, messages, run input/output/error, tool payloads,
  arbitrary metadata, saved execution targets or secrets/config payloads. Known
  active credentials are redacted from allowed text; controls/bidi are sanitized.
  Names/titles and identity details remain user data: do not publish output blindly.
- **Pagination:** one page by default. Runtime supports unpaginated lists and DRF
  **page-number** pagination (`--page`, optional host-supported `--page-size`).
  Studio threads support `--limit` (1–100), `--offset`, `--project-id`, `--archived`.
  Use `--all --max-pages 10` to follow up to ten pages (hard maximum 100). Unknown
  pagination, changed filters/counts or repeated rows fail rather than silently
  return incomplete success. Retry reads if concurrent changes invalidate a scan.
  Runtime counts may include expired rows omitted from returned metadata.
- **Status:** probes configured agent/project lists and optional identity, at most
  three GETs. Reports observed availability, **not** advertised capabilities or an
  authorization grant. Unavailable probes give a nonzero exit. `whoami` requires
  an explicitly configured host-approved read-only identity path; no guessing.
- **JSON:** stdout is `{schema_version: 1, data: ...}`. Lists contain `items`,
  `count`, `has_more`, `pages`. Errors are JSON on stderr when `--json` is present;
  stdout remains empty on ordinary failure. Status returns its observations even
  when a probe fails. Human tables truncate long cells; JSON retains allowed text.
- **Transport:** verified HTTPS only; `--allow-http-loopback` is an explicit local
  HTTP exception, never a remote insecure mode. No redirects (even same-origin),
  proxies, cookies, netrc, anonymous fallback or retries. Pagination cannot leave
  the exact list endpoint/origin. 4 MiB response limit, JSON depth 64, socket
  timeout 15 seconds (`--timeout`, maximum 120). This is not a total wall-clock
  deadline. Error bodies/headers and underlying exception details are never shown.

Exit codes: **0** success, **2** arguments/config, **3** credential/auth/permission,
**4** unsupported/not found, **5** transport/local I/O, **6** protocol/pagination,
**7** other HTTP errors (including rate limits) or unsuccessful status, **130** interrupt.

## Deliberately not included

- Run detail GET: current runtime `retrieve()` can schedule queued execution.
- Executions, writes, migrations, SSH, direct database or management commands.
- SDLC commands: its current API is session/CSRF-only. Add a supported host-approved
  non-browser auth adapter and ACL tests first; do not turn tokens into sessions.
- Browser/device login, live production acceptance, server capability discovery,
  Parrot/ACE/Warp commands, cursor/limit-offset runtime pagination, plugin loading.

## Extension / verification

`config.py` owns local metadata/credentials, `transport.py` owns HTTP safety,
`resources.py` maps existing REST contracts, and `cli.py` owns syntax/output.
Add new command groups by wrapping supported APIs, not copying business logic.
New writes need server revision/idempotency rules and explicit safety semantics;
do not bolt a generic arbitrary-request command onto this read-only client.

Run CLI tests using the root Python (isolated environment):

<augment_code_snippet mode="EXCERPT">
````sh
env -i PATH=/usr/bin:/bin PYTHONPATH=clients/agent-cli/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -c clients/agent-cli/pyproject.toml clients/agent-cli/tests -q --tb=short --assert=plain
````
</augment_code_snippet>

Real runtime/Studio authentication, ACLs, defaults and read-only SQL/dispatch
contracts are exercised separately: [integration test instructions](integration_tests/README.md).
The CLI suite also uses a temporary loopback HTTP server with synthetic data.
No live credentials or host database are used. Rerun both suites when extending
commands. See [the inventory and implementation record](../../plans/studio-runtime-cli.md).

### Validation record (2026-09-17)

- 353 CLI/config/transport/metadata tests passed, plus 65 unittest subtests,
  including five executable-rename regressions.
- 29 real runtime/Studio API contract tests passed on the root environment and
  again with the existing isolated Django 5.2 overlay; no host database access.
- Both suites also passed using the installed wheel instead of the source tree.
- Offline wheel build, zero-dependency metadata/source verification, clean-venv
  entrypoint/help/version/profile smoke checks and Django-free import passed.
- Isolated upgrade from the previous wheel verified the `studio` executable,
  removal of the old `agentctl` executable, and preservation of existing profiles.
- Package-scoped Ruff checks passed. No production login or remote deployment was
  performed. Provision approved credentials before a separately authorized remote
  acceptance check; add and run regression tests for each new API integration.
