# Anywhere control plane: reuse-first integration boundary

Status: source audit and proposed direction, 2026-09-13. Not a deployment or
an assertion of end-to-end production readiness. No remote machines enrolled.

**2026-09-15 update:** the sections below retain the original audit snapshot.
Remote ACP handoff has since been implemented; see
[the rollout record](../packages/python/agent_runtime_core/deploy/ROLLOUT-2026-09-15.md).
The confirmed workspace direction is now the
[Studio-owned multi-location contract](../packages/python/django_agent_studio/CONTROL_PLANE.md):
one server-owned master list of explicitly added virtual pointers, shared by web,
desktop and future mobile clients. No proxy-owned master catalog, automatic root
scan or project-imposed filesystem isolation. The new contract supersedes earlier
workspace-root/discovery proposals; it does not claim the pointer UI is shipped.

## Product direction

Studio is the always-available workspace and operations UI. Its existing web
widget talks to the authenticated runtime API/event stream. The runtime either
executes locally or delegates to a remote agent through an adapter. Files and
terminals are negotiated remote-workspace capabilities, not implied by chat.

Recommended route: browser/widget → Django runtime → local execution **or**
`RemoteACPRuntime` → reachable remote ACP endpoint/connector → agent.
Remote machine workspace services form a companion channel behind the same
authorization boundary. Do not put remote-machine credentials in the browser.

## Existing source to reuse

| Layer | Existing implementation | Boundary / remaining integration |
| --- | --- | --- |
| Web client and chat | `clients/agent-frontend/packages/agent-client/src`, widget `src` | Existing runtime HTTP/SSE, history, cancellation, attachments and event handling. Not a native browser ACP client. Extend reusable packages, not a Studio-only chat fork. |
| Outbound ACP | `packages/python/agent_runtime_core/agent_runtime_core/acp/remote.py` | `RemoteACPRuntime`, WS and HTTP transport factories, event mapping, session checkpoint/load, cancel and deny-by-default permission callback. Current prompt forwarding is text-only and advertises no filesystem/terminal client capabilities. |
| Database agent adapter | `packages/python/django_agent_runtime/runtime/remote_acp.py` | Active-version `acp_*` configuration builds the existing remote runtime. Add safe connection administration around this, rather than another execution engine. |
| Inbound ACP | `packages/python/django_agent_runtime/acp_server/{asgi,agent}.py` | Authenticated HTTP/WS mount, conversation sessions and event translation. Useful to expose our runtime to external ACP clients; not required merely for outbound federation. |
| Editor relay | `packages/python/agent_runtime_core/agent_runtime_core/acp/bridge.py` | **Client-side** stdio → remote WebSocket relay. Does not publish a laptop's local stdio agent to the cloud. |
| Local coding agent | `products/jimmy/src/jimmy/{acp,workspace,security}.py` | ACP stdio and permission/workspace boundaries. Its client-buffer workspace is proposal-only. A remote endpoint/connector is needed for cloud access; do not assume the cloud has its local files. |
| Remote execution | `packages/python/agent_runtime_core/agent_runtime_core/execution/{provider,conduit_provider,conduit_client,tools}.py` | Session commands/output, sync/async command execution, heartbeat and file read/write primitives. Existing HTTP polling/capture is not a complete browser terminal transport or resumable transfer service. |
| Session persistence | `packages/python/django_agent_runtime/conduit/persistence.py`, `models/concrete.py` | Existing `ActiveSession` supports run-associated remote session tracking. A user-facing workspace session can outlive a run; extend the owning layer after defining that lifecycle. |
| Tools and distribution | Core/runtime MCP, Chisel/Django Chisel, Parrot | Reuse tool integration and versioned artifact distribution. MCP tools are not agent sessions; Parrot artifacts are not a live machine-presence registry. |

## Ownership rules

- **Core:** protocol/transport adapters, remote execution interfaces, capability
  translation and portable session behavior; no dependency on Studio or Django.
- **Runtime/host:** connection identities, credential references, ACL enforcement,
  durable runs/history, session bindings, approvals, audit and network policy.
- **Client/widget:** reusable stream rendering, actions/approvals, capability-driven
  UI and reconnect UX. Keep transport specifics out of view components.
- **Studio:** organization/project presentation and control surfaces. Preserve its
  removable overlay model; runtime must remain usable when Studio is uninstalled.
- **Remote connector/provider:** own the remote workspace root, agent processes,
  terminal/file operations, local enforcement and narrowly scoped credentials.

## Connectivity and trust

1. Start with reachable authenticated TLS endpoints using the existing transports.
   An ACP stdio implementation does not automatically support these network
   transports; negotiate supported protocol and capabilities through its adapter.
2. Laptops behind NAT/firewalls need a VPN/private route or a connector that dials
   outward to the control plane. Mere internet connectivity on both ends is not
   enough. An outbound connector/reverse route is integration work, not something
   supplied by the current editor relay.
3. Enroll stable endpoint/workspace identities with explicit owner and project
   grants, revocation, credential rotation and auditable actions. Constrain egress
   destinations (including redirects/DNS/private metadata addresses) to prevent
   arbitrary endpoint configuration becoming SSRF. Private destinations require
   deliberate network policy, not a blanket bypass.
4. Keep remote tool permissions denied until the existing runtime/client action
   lifecycle is connected to a scoped ACP permission decision. Do not auto-approve
   to make a demo work. Validate decision identity, scope, expiry and revocation.
5. Remote paths always refer to the enrolled remote workspace. Never accidentally
   interpret an ACP filesystem callback as access to the cloud host's filesystem.
   File attachments, ACP client-buffer reads and remote disk transfers are distinct.
6. Define whether transcripts, patches and transferred files may persist in the
   cloud. Respect host retention/sovereignty policy; no convenience storage override.

## Session and reliability contract to finish

- Keep Studio thread, local conversation/run, remote agent session, remote workspace
  and terminal identities distinct and explicitly bound to connection + principal.
- Define browser disconnect separately from execution cancellation. An offline
  remote machine must remain visibly offline; do not blindly replay a prompt.
- Verify reconnect/history replay, cancel/timeout propagation, concurrent prompts
  and lost-session recovery. The current ACP client can fall back from failed load
  to a new session; decide when that must instead require explicit user action.
- File UI requires negotiated list/read/write/transfer capabilities and safeguards
  for paths, symlinks, sizes, binary data and conflicting edits. Extend the provider
  abstraction where needed; file methods are not currently on its abstract base.
- A browser terminal additionally needs input/output streaming, resize, exit status,
  backpressure, detach/reconnect and scoped lifetime. Reuse provider primitives,
  but do not label polling command output a finished remote IDE.
- Always-online service availability needs durable state outside web processes.
  ACP connection registries/owner maps are currently process-local; address routing,
  restarts and multi-instance behavior before claiming cloud failover continuity.

## Focused implementation sequence

1. Align the local host environment and reload-enabled web/worker startup after
   checking active work. Frontend watchers are separate and safe to start now.
2. Add a provider-free end-to-end fixture: widget/runtime → authenticated remote ACP
   echo server. Test multiple turns, reload without duplicate history, cancellation,
   remote outage and cross-user isolation. Existing unit tests are not this test.
3. Connect one real remote agent through a deliberate route and capability policy;
   bridge scoped user approvals without changing deny-by-default behavior.
4. Add file and terminal capabilities through existing execution providers and an
   optional remote connector. Then test cloud multi-instance/reconnect behavior.

## Local development findings

`make watch-frontends` watches canonical client TS + widget assets and Studio Vue;
refresh the browser after builds (not HMR). Both watchers were started and six
served assets matched current build hashes. Python imports in the existing host
resolve to local core/runtime/Studio/Chisel/Django Chisel/Parrot source checkouts.
However, existing web/worker processes still use `--noreload`; the root test venv
lacks some host dependencies and the host venv lacks the optional ACP package.
The host project's ASGI source currently mounts only Django. The Google Cloud
deployment/revision was not inspected or modified. Production uses released
artifacts and a controlled deployment, not local development watchers.

## Decisions requested

- First remote target: another runtime server, Jimmy on a laptop, or a third-party
  ACP agent? A reachable second runtime is the smallest initial vertical slice.
- Must NAT/private laptops work immediately? If yes, may a small connector run on
  them, or should we use an existing VPN/private network first?
- May remote transcripts and selected files persist in the control plane, or do
  some connections require remote-only/limited persistence?
- Should v1 include writable files and an interactive terminal, or begin with chat
  plus approved file transfer? These require explicit permissions either way.