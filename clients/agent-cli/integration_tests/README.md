# Existing-API integration contract tests

These tests exercise `agentctl.transport.Client` and `agentctl.resources.ReadAPI`
against actual runtime/Studio REST views with actual DRF token authentication.
They are **in-process REST contract tests**, not live-server/TCP/TLS tests:
only `HTTPConnection.request/getresponse` are bridged to Django's test client.
Client target/query/header construction, status handling, body validation, JSON
decoding, metadata projection and pagination remain real. No `force_authenticate`
or fake REST responses are used.

## Run in this checkout

From the repository root, using the existing root virtualenv:

<augment_code_snippet mode="EXCERPT">
````sh
env -i PATH=/usr/bin:/bin PYTHONPATH=agent:clients/agent-cli:clients/agent-cli/src PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_django.plugin -p no:cacheprovider -c /dev/null --ds=integration_tests.settings clients/agent-cli/integration_tests -q --tb=short --assert=plain --no-showlocals
````
</augment_code_snippet>

Append `-k supported_thread_creation` for the smallest default-path check, then
run the complete directory. The clean environment avoids inheriting host
credentials/settings or pytest options. Short tracebacks, plain assertions and
disabled locals prevent credentials from appearing in assertion diagnostics.
Do not enable request/SQL debug logging, locals, HTTP debug traces or token dumps.
No dependency, package, host database or manifest changes are required.

## Isolation and contract boundaries

- Settings inherit **only** `django_agent_studio.tests.settings`, verify its
  in-memory SQLite database and use a test-only URLconf. Collection rejects any
  other settings module. An audit hook denies all socket traffic for this test
  process; even the loopback origin is bridged, never contacted.
- Runtime base views delegate auth to hosts. Test-only subclasses explicitly use
  `TokenAuthentication` and `IsAuthenticated`; Studio's existing workspace views
  retain their own policy. Studio's session-first auth returns 403 for rejected
  tokens; the token-only runtime/identity mounts return 401.
- Users/tokens are disposable ORM fixtures. Agents, organizations, projects,
  memberships and threads are created through authenticated supported REST
  writes. Sharing/revocation uses the real workspace mutation APIs.
- General setup omits optional storage, history, target, sharing and model
  settings. Tests verify blank agent storage configuration and runtime-initialized
  normalized, empty history. A named local host-JSON compatibility case checks
  inheritance without changing existing pinned conversations.
- The named queued/no-worker fixture POSTs runs through real runtime creation,
  mocking only dispatch scheduling during that POST. It creates no synthetic
  completed history and performs no provider/model work.
- Every adapter request must be GET. Database execution wrappers reject writes
  during REST reads, including on-commit callbacks. Dispatch scheduling and
  accepted-run dispatch are forbidden outside the narrowly scoped setup mock.
  The real unsafe run-detail route is mounted: `ReadAPI.get` must refuse it
  **before transport**, not merely receive a 404 from a missing route.
- Coverage includes invalid/revoked/inactive credentials, metadata projections,
  owner-scoped runs, guest/cross-project permissions, private/selected/project
  shares, project/organization revocation, creator ACL revocation without loss
  of runtime ownership, pagination/filtering, alternate mounts, optional identity
  and read-only status observations.

The command-entrypoint test also covers parser/profile/environment-credential
routing, JSON output and token revocation through the actual Studio API. A named
paginated-host test uses real DRF page-number pagination on the runtime view.
These tests do not exercise credential-file lifecycle, real HTTP framing/TLS,
a deployed host policy or database concurrency. CLI credential/transport tests
cover their respective boundaries; the CLI unit suite includes real loopback HTTP.
