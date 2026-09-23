# Multi-network device onboarding

## Contract and boundaries

One installation may use several Tailscale networks and routed private VPNs,
including Azure VPN. This is connector routing support, not VPN provisioning.
No production configuration, routes, services, secrets or persisted grants are
changed by this implementation. Existing personal onboarding stays compatible.

Runtime owns connection/enrollment identity. Studio owns organization policy.
Runtime never imports Studio; a configured policy backend bridges the boundary.
Org administration does not grant control of a member's laptop or private history.
Local pairing consent and owner-only execution remain required. Coding agents are
not filesystem sandboxes; browse grants and execution permission stay distinct.

## Host-approved profiles

The existing DJANGO_AGENT_DEVICE_ONBOARDING dictionary remains the host gate.
Legacy single-network keys remain supported, with blank network_id and scope
identifying legacy records; their signatures and configuration digest do not change.

Multi-network form uses ENABLED, PUBLIC_BASE_URL, PACKAGE_URL, PACKAGE_SHA256,
POLICY_BACKEND (optional dotted class), and NETWORKS (mapping keyed by stable slug).
Each profile contains NAME, KIND (tailscale, azure_vpn, private_vpn), ALLOWED_CIDRS,
SSH_IDENTITY_FILE, SSH_PUBLIC_KEY, SSH_PROXY_COMMAND, plus TAILNET_LABEL for
Tailscale. Optional OWNER_IDS supplies standalone runtime authorization only when
POLICY_BACKEND is absent. Each SSH proxy command has exactly one %h and %p.
No browser-supplied shell command, identity path, route or artifact URL is accepted.

CIDRs are explicit IPv4 subnets of RFC1918 or, for Tailscale, 100.64.0.0/10.
Public, loopback, link-local, metadata and overly broad ranges are rejected.
Every connection selects its saved profile, never a default/fallback route.
Overlapping address ranges require distinct host-provisioned proxy routes; identical
SSH_PROXY_COMMAND with overlapping ranges is invalid. Operators must actually
isolate those routes (separate Tailscale sockets, routing namespaces or gateways).
Distinct commands alone do not prove network isolation.

Transport digests include the selected profile and its stable ID, not OWNER_IDS,
other profiles, org membership or display name. They include the selected policy
backend identity so removing/changing authorization backends cannot loosen an
existing binding. Route/key changes fail closed and require reviewed re-enrollment.

## Runtime policy backend and organization policy

Runtime configuration helpers:
- configurations(): mapping of network ID to validated profile; legacy key is ''.
- configuration(network_id=''): validated profile or None.
- require_config(network_id=''): profile or DeviceUnavailable.
- config_digest(config): legacy digest unchanged, v2 transport-only digest.

A POLICY_BACKEND class is instantiated without arguments and implements:
- scopes(user, network_id): list of {id: string, name: string}; active grants only.
- authorize(user, network_id, scope, *, action, run=None): returns exactly True
  or denies. Runtime catches errors and denies; checks current policy on each use.
Actions are enroll, use and execute. execute receives the actual persisted run.
No backend means per-profile OWNER_IDS and one blank, personal scope.

Studio backend: django_agent_studio.services.device_networks.DeviceNetworkPolicy.
StudioOrganizationDeviceNetwork binds an existing organization to a host profile.
Only a platform operator may provision that assignment via Django administration;
org admins cannot self-assign another organization's network. No automatic grants.
The assignment has enabled=False and enrollment_role='admins' initially. Its org
owner/admin may change enabled and enrollment_role ('admins' or 'members') through
the supported organization API/UI, without deployment. Role applies to enrollment
and continued device use; revocation or loss of qualifying membership denies use.
Preserve assignment rows when disabled; audit actor/time and policy revision.

Network policy API under workspace/organizations/<key>/device-networks/:
- GET: {networks: [{id, name, kind, enabled, enrollment_role, revision}]}.
- PATCH: {network_id, enabled, enrollment_role, revision}; optimistic conflict check.
Both require current owner/admin. Only assigned and valid host profiles are shown.
WorkspaceBackend has get_organization_device_networks and
set_organization_device_network methods; default backend delegates, others deny.

Organization scope is an immutable pairing/device field (opaque to runtime).
Studio execute checks actual conversation/thread organization when present; a
thread in another org is denied, including if the user belongs to both. Owner-only
session preparation/directory operations without a thread may proceed only under
their existing runtime fences and current selected-org device authorization.
Runtime invokes execute policy at the persisted-run binding fence on send/receive.
Already-sent remote actions cannot be recalled; no stronger cancellation is claimed.

## API and connector protocol

Existing capabilities response stays byte-shape compatible in legacy mode.
Multi-network capabilities adds networks, each {id, name, kind, scopes,
install_url, manifest_url}; scopes are policy-authorized {id, name} options.
Top-level install_url/manifest_url are null in multi mode, never an implicit default.
Pairing POST is {name, network_id, scope} in multi mode; legacy {name} still works.
Device responses add network_id and scope for new profiles, without connection data.
New model fields default blank; legacy signatures omit them exactly as before.
Pairing binds both network and scope; redeem cannot choose or substitute scope.

Public network-specific routes:
- onboarding/networks/<network_id>/install.py
- onboarding/networks/<network_id>/manifest/
- onboarding/networks/<network_id>/redeem/

Manifest v2 is exact:
version=2, package_url, package_sha256, ssh_public_key, redeem_url, network.
network is {id, name, kind, allowed_cidrs, tailnet_label}; tailnet_label is null
for routed VPNs. Manifest/redeem share canonical HTTPS origin; no redirects.
Redeem v2 adds network_id to the existing payload; route, payload, ticket must agree.

Installer preserves v1 unchanged. V2 Tailscale checks the exact selected tailnet.
V2 routed VPN requires explicit local --address IPv4 (or interactive apply input),
checks CIDR and that the address can bind locally, then probes SSH on that address.
It does not inspect VPN credentials or pretend a local probe proves worker access.
No VPN install/account switch/service start/route edit. Worker verification remains
mandatory. Each v2 profile uses separate private local state derived from HTTPS
origin and network ID, without adopting existing v1 state or sessions. Network
keys must differ for separate local forced commands; conflicting keys fail closed.

## Verification and rollout

Isolated tests must cover legacy records/signatures; multiple profiles; same IP
over distinct routes; mixed Tailscale/Azure; cross-network code substitution;
CIDR rejection; network/key drift; unrelated profile/owner changes; scope grants;
org/member revocation; worker send/receive authorization; no Studio dependency;
connector preview/consent/reruns and private state; UI stale selection/pairing.
Use root .venv Python and isolated Django test settings, never host databases.

Before production: review migrations, publish a real connector wheel and checksum,
provision isolated routes/keys, create explicit org assignments, deploy matching
runtime/core/Studio, then perform approved real worker verification for EACH network.
Unit tests and saved policy are not evidence of VPN reachability.

### Local validation completed

- Runtime regression selection: 497 passed, 1 skipped.
- Core connector regression selection: 302 passed.
- Full Studio Django suite: 1,658 passed, including the real runtime binding test
  for current thread organization and immediate policy revocation.
- Frontend: 428 passed; Vue/TypeScript typecheck and Vite build passed.
- Django system check and runtime/Studio migration consistency checks passed.
- Tracked whitespace checks passed in all three packages.

All commands exited 0; Django tests used isolated settings and the root virtualenv.
The Django runs reported an existing skills CheckConstraint deprecation warning.
These results do not include a browser, live worker/VPN, production migration or
deployment test. Add deployment-specific route acceptance tests and run them with
approval before enabling each real network.