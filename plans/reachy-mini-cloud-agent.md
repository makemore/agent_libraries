# Reachy Mini: cloud agent, live voice and lightweight robot bridge

Status (2026-09-21): local Studio voice and controlled onboard bridge tested;
speaker buffering installed and user reports improvement. No cloud rollout.
Motion/vision capability inspection complete; implementation and activation are
pending. Earlier findings below describe the initial design, not current rollout
state; see the dated motion/vision scope and client deploy/LOCAL.md for updates.
Target agent host: https://studio.makemoredigital.com/.

## Recommendation

Treat Reachy as the physical interface to a Studio agent, not as the agent host.
Keep reasoning, memory, tools and vision inference off the robot. Keep hardware
limits, interpolation, bounded animation playback and disconnect safety local.
The custom application is principally a media/control bridge, but not a blind
pipe: ownership, cancellation, backpressure and verified action results matter.

The bridge lives in **`clients/agent-reachy/`**, a new independent local Python
client repository alongside the other agent clients. No remote/submodule pin is
configured yet. It can run as a cloud service or developer CLI; media dependencies
and deployment remain separate approval gates.

User-confirmed hardware: **Reachy Mini Wireless**, reachable from this computer
over Wi-Fi, with both the robot and Studio cloud worker on Tailscale. Initial
validation will use local Studio at `http://127.0.0.1:8001/`. Read-only checks now
confirm SSH and daemon 1.9.0 at `100.119.90.71`, plus local media-signaling discovery.
Actual media connectivity remains unverified. Tailscale may provide a private
direct path, but does not itself prove SDK/WebRTC compatibility or replace
robot ownership checks. Validate direct Wi-Fi/Tailscale SDK/media access before
assuming HF central signaling is required; retain the central-consumer alternative.

## Findings: what already exists

### Runtime voice foundation

- `packages/python/agent_runtime_core/agent_runtime_core/live.py` contains
  `OpenAILiveProvider.create_webrtc()`, outbound `sideband()`, `build_live_config()`,
  `LiveTranscript` and bounded public `commentary_events()`.
- This targets **GPT-Live / `gpt-live-1`**, not the older Realtime protocol and not
  a text-to-speech-only service. Current config uses client delegation: our backend
  agent performs the work; OpenAI provides the full-duplex conversational voice.
- `packages/python/django_agent_runtime/voice/live_views.py` already supplies
  host-subclassed create/status/close views. Creation accepts only an SDP offer
  and optional conversation ID; agent selection is server-owned.
- `voice/live_context.py` prepares the canonical policy-checked prompt and snapshots
  the reachable agent configuration. `voice/live_delegation.py` creates ordinary
  backend runs and fences stale tool execution. `voice/live_worker.py` owns the
  outbound sideband, transcript/history finalization and cumulative usage.
- `models/live.py` and migrations 0048/0049 already exist. The separate supervised
  `runlivevoice` management command is required; an HTTP thread is not the worker.
  Existing Live integration does **not** require an inbound application WebSocket.
- Source defaults: Live disabled, tool access `manual`, maximum session duration
  900 seconds, one session per owner, and a required trusted owner-revocation hook.
  Admission requires eligible persistent normalized history and rejects restricted
  egress/private-only contexts. Preserve these contracts and existing overrides.
- Initial inspection found no Live host routes or browser Live peer. The first
  slice below now adds host routes; browser/robot peers are still absent. Local
  source inspection does not establish production state.
- The current provider factory is OpenAI-specific. Existing TTS provider classes
  are a separate abstraction, not interchangeable full-duplex Live providers.

### Reachy foundation

- The daemon handles camera/audio hardware, motor safety and local interpolation.
  Remote media uses WebRTC with H.264 video and Opus audio; REST is appropriate
  for setup/status, not for transporting a live stream as repeated requests.
- Upstream `reachy_mini.media.central_consumer.ReachyCentralConsumer` is a
  hardware-free cloud client using `aiortc`/`av`. Current source exposes camera
  `latest_frame()`, microphone `on_pcm`, speaker `out_track`, and `send_command()`.
  Its narrative cloud guide lags the source's bidirectional audio support.
- This permits a cloud-owned robot connection without running our agent stack,
  Django, a browser, or model inference on the robot.
- Important gaps: incoming data-channel messages are currently ignored;
  `send_command()` means queued for sending, not acknowledged/completed. We need
  telemetry and correlated action results before reporting successful movement.
- Central signaling permits one consumer per robot. A browser preview must not
  create a second competing peer; fan out preview from the bridge or hand off
  ownership explicitly. HF robot ownership and Studio ownership are distinct.
- Validate identity pinning across failures: the inspected `_start_session()` can
  clear a rejected target and discovery can select by name. Our adapter must fail
  closed/re-authorize instead of silently attaching to a different robot.
- Upstream documents aiortc/Cloudflare TURN caveats and includes a DTLS workaround.
  Pin and test a compatible released SDK/daemon combination; do not assume all
  features visible on `main` are already available in an installed release.

## Architecture and transport

### Three independent responsibilities

1. **Studio agent/control plane:** owner authorization, robot binding, conversation,
   prompt, tools, memory, approved vision inference, audit and session admission.
2. **Reachy bridge/media worker:** one robot peer per authorized session; audio
   routing, bounded frame buffer, control delivery, telemetry and connection health.
   Run it as a supervised long-lived process, not a Django request or an LLM tool
   that remains blocked for the whole call.
3. **Robot daemon / optional tiny local guard:** capture and playback, smooth
   trajectories, local motion bounds and stop behavior independent of cloud latency.

### Initial transport choice

- **Robot ↔ bridge:** Reachy WebRTC media plus its command data channel, negotiated
  via central signaling. Media flows peer-to-peer or via TURN, not through the
  signaling service. This is the proposed low-latency/high-throughput path.
- **Bridge ↔ OpenAI:** initially a second WebRTC audio peer, reusing the existing
  runtime's SDP bootstrap and server-owned sideband. The bridge acts as the media
  client, supplies robot microphone audio and forwards received audio to Reachy.
  It receives no OpenAI project API key. Separate SDP negotiations are required:
  the robot and OpenAI connections cannot simply share an SDP or be spliced together.
- **Studio ↔ OpenAI:** existing outbound sideband for transcripts, delegation and
  public backend results. The media bytes do not pass through Django REST/SSE,
  the run event bus or durable task payloads.
- **Bridge ↔ Studio control:** proposed bridge-initiated authenticated WSS for
  leases, commands, cancellation and results. Bind it to an admitted owner/robot/
  conversation; use HTTPS for enrollment and session bootstrap. This is a new
  robot-control transport and requires explicit host ASGI/gateway wiring, unlike
  the existing voice sideband. Do not expose robot port 8000 to the Internet.
- Place the cloud bridge near the agent host/provider region, then measure actual
  latency and TURN use. Transport names alone provide no latency guarantee.

OpenAI also documents a **primary GPT-Live WebSocket** for server audio. It may
simplify a cloud bridge (PCM in/out instead of a second media peer), but the current
runtime implements WebRTC bootstrap plus sideband, not primary audio streaming.
Treat this as an alternative spike requiring lifecycle/provider work, not a
drop-in call to `sideband()`. Avoid supporting both paths in the first release.
If “high speed” meant FastRTC specifically, evaluate it against this spike rather
than introducing another media framework before it is needed.

### End-to-end behavior

1. User authorizes a specific robot and Studio agent. Server creates an expiring
   binding/lease after checking both authorities; no model-selected robot IDs.
2. Bridge negotiates robot media and creates the voice SDP offer. The host extends
   `BaseLiveSessionView.reserve_session()` to associate an authorized robot session
   atomically, rather than accepting arbitrary client policy/model/tool fields.
3. Robot microphone feeds GPT-Live; returned voice plays on the robot speaker.
4. Live delegates a request through the existing sideband controller to the Studio
   agent. The agent uses ordinary authorized tools, including Reachy tools.
5. Reachy tools send bounded commands or request current observations. Confirmed
   results return to the agent and then to Live as speakable commentary.
6. Close, owner revocation or lease loss ends voice, invalidates pending commands,
   clears audio queues and invokes the validated local safe-stop policy.

## Vision, motion and voice details

### Vision is a separate backend capability

GPT-Live 1 supports audio/text, **not image/video input**. Its existing runtime
history projection is text-only too. Stream camera video to the bridge, but begin
with an on-demand `inspect_scene(question)` tool over a fresh bounded snapshot.
Attach capture/receive timestamps, frame ID and pose when available; reject stale
frames. Report uncertainty rather than claiming continuous awareness.

The core already offers `files/vision.py::VisionProvider.analyze_image()` and
`VisionResult`. Reuse through a host-authorized provider route; do not bypass
egress, model or credential policies by constructing an unrestricted client.
Return a bounded description/evidence reference to the agent, not raw provider
responses. Tool `contentBlocks` are presentation metadata, removed from the LLM
payload in `agentic_loop.py`; an image displayed in the UI does not prove the
reasoning model received it. Test actual vision input explicitly.

Later add opt-in change detection, low-rate observations and tracking outside the
LLM loop. Keep a latest-frame buffer; never accumulate a video backlog. Do not
persist raw video/audio by default or silently create public image URLs.

### Motion is intent, not cloud motor streaming

Proposed tools: `get_robot_state`, `inspect_scene`, `look_at`, `play_animation`,
`stop_motion`. No arbitrary Python, shell, raw motor register or unrestricted
daemon-administration tools. Use safe named animations and bounded pose/duration
commands. Prefer daemon-side `goto_target` and recorded-move playback; reserve
high-frequency `set_target` for a dedicated local controller, not an LLM over WAN.

Each command needs a command ID, lease/session generation, expiry, bounds and
states such as accepted/running/completed/cancelled/failed/unknown. Dedupe retries;
do not replay motion on reconnect. Track actual acknowledgments/telemetry and
report unknown outcomes honestly. New user speech can invalidate a Live delegation;
extend that fence to queued robot commands, without claiming a started action was
undone. Only one motion scheduler owns the robot, including idle/tracking behavior.

Local loss-of-control behavior is a release gate. Test the daemon's watchdog;
if insufficient, add a tiny local guard instead of relying on a cloud stop packet
that cannot arrive after a network failure. Define a hardware-appropriate stop
and safe pose; do not equate disabling torque with a universally safe stop.

### Audio and expression

- Negotiate/query real sample rates and channels. Bridge explicit formats rather
  than assuming every path is 16 kHz; the central consumer currently defaults its
  microphone callback to 24 kHz mono float32.
- Bound capture/playback queues, preserve ordering, detect stalls, and drop/reset
  stale output on interruption. Validate echo cancellation with the robot speaker
  and microphone; do not accidentally capture the operator laptop microphone.
- Full duplex is not the same as reliable barge-in across two media legs. Measure
  interruption behavior and robot buffer clearing on actual hardware.
- Drive subtle speech motion from the playback clock/envelope, not transcript
  arrival. Transcript deltas and commentary acknowledgments are not proof of
  playback. Recorded synchronized A/V and live speech have different clocking needs.
- Keep voice and motion providers independent. Introduce a small live-session
  protocol/capability boundary with OpenAI as the first adapter; retain its actual
  delegation/cancellation semantics. A second voice provider can be added without
  changing robot tools. Do not force full-duplex providers through the TTS API.

## Motion/vision implementation scope — inspected 2026-09-21

This section records a proposal, not installed capabilities. Inspection used
source and OpenAPI only: no movement, camera capture, tracking enablement or
service changes. The current voice/audio implementation is left unchanged.

### Verified integration points

- Database tools belong to AgentDefinition through AgentTool/DynamicTool, not
  to a new independent voice agent. Studio's supported
  `services/builder_mutations.py::add_tool(agent_id, args, user=None)` supplies
  preview, validation and revision history. Use a reusable targeted provisioning
  routine with isolated tests; preserve existing prompts, tools and settings.
- Trusted wrappers can use
  `dynamic_tools.context.get_tool_execution_context()` to resolve the persisted
  run/conversation and recheck owner/device binding. Never accept caller identity,
  robot address, import path or approval flags from model arguments.
- Text and Live dispatch through DynamicAgentRuntime. Live tool authorization
  fences stale delegations and defaults to manual. AgentTool safety flags or a
  prompt instruction do not authorize an action. A real action-bound approval
  flow or explicitly approved, narrowly scoped automatic policy is still needed;
  do not silently set host-wide LIVE_TOOL_ACCESS to auto.
- No first-class database Skill model was found in the inspected runtime/Studio
  paths. Start with database tool records and a managed prompt section explaining
  when to gesture, inspect a scene, and admit uncertain observations.
- The installed daemon exposes goto with duration, running move UUIDs and
  per-UUID cancellation. Recorded dataset playback exists, but do not expose
  arbitrary dataset names/downloads or trust third-party trajectory bounds.
- `/api/state/doa` reports radians (0 left, pi/2 front, pi right) and speech
  detection; initial readings may be absent. Mapping to head/body coordinates,
  sampling freshness and rejection of robot-speaker echo need testing.
- Daemon tracking consumes its existing local shared GStreamer camera feed.
  `/api/media/tracking/enable`, `/disable` and `/face` exist. An enabled response
  is not proof the detector thread started successfully or found a face.
- Camera specs exist, but no snapshot/frame REST route was found. Implement a
  bounded, on-demand local camera adapter without releasing hardware or
  re-enabling incoming video on the audio peer (which previously caused loss).
- Core `VisionProvider.analyze_image()` is an inference primitive, not an
  automatically policy-enforced Studio connection. The existing OpenAIVision
  constructor does not expose a custom base URL. Resolve an image-capable model,
  credentials, endpoint, egress authorization and usage through the host's
  supported provider path; no ambient-key fallback. Tool contentBlocks are UI
  metadata removed from LLM input, so image display alone is not a vision test.

### Proposed first slice

1. Host wrappers in products/studio/reachy for `play_animation(name)`, bounded
   `look_at`, one-shot `face_speaker`, `inspect_scene(question)` and `stop_motion`.
   Begin with small vetted nod/shake/antenna gestures, no body rotation or sounds.
   Choose and validate numeric bounds before any physical acceptance test.
2. Add a correlated command/result channel to the device-bound bridge, with
   single-controller ownership, run/device generation, expiry, idempotency and
   explicit terminal/unknown outcomes. Recheck authority at dispatch and at the
   edge; a timeout must never cause an automatic replay. Reuse existing binding
   and control contracts, not direct cloud access to the daemon port.
3. One edge motion arbiter owns gestures and attention. Face-speaker first uses
   fresh speech-direction evidence, then briefly refines with local face tracking
   if camera consent is present. Multiple faces/noisy sound remain ambiguous;
   centering a face is not active-speaker recognition or personal identification.
   Defer continuous tracking until the bounded one-shot path is measured.
4. Keep local-camera and external-image-analysis consent distinguishable. Cloud
   inspection captures one fresh size-limited frame for an authorized request;
   no continuous upload, public URLs, raw bytes in tool/audit records or new raw
   image persistence. Text results follow existing conversation retention. Image
   provider retention must not be described as under our local deletion control.
5. Add visible motion/camera controls, off by default. OFF/revocation/lease loss
   cancels owned work and clears pending observations. Never stop another app's
   moves or assume torque-off is a safe stop. Validate local crash/watchdog
   behavior, not just a cloud stop request; continuous daemon tracking must not
   outlive its approved session after the bridge fails.

### Test and rollout gates

- Isolated host tests: default manual denial, explicit approved scenario,
  unbound/cross-owner/forged context denial, stale Live run and binding rotation,
  duplicate command/result handling, policy/egress denial before capture/upload.
- Provisioning preview/apply/rerun tests through supported services and read
  APIs. Do not reset storage, model, sharing, existing tools or user prompt edits.
- Edge fakes: finite bounded inputs, stale/absent DoA, echo, ambiguous/no face,
  tracking-vs-gesture arbitration, cancellation during awaits, disconnect/crash,
  no replay or delayed action after OFF. No network or hardware in unit tests.
- Vision tests must assert actual image bytes reach the authorized fake provider,
  with bounds/freshness and no raw image in persisted results/logs. Add named
  privacy/unsupported-provider cases, without changing general fixtures.
- Rerun audio regressions, then approved bounded physical tests: one gesture,
  stop while moving, one snapshot, one speaker-facing attempt, followed by CPU
  and audio checks. Do not claim tracking precision or cancellation latency from
  source inspection. Ask before adding dependencies or changing robot services.

Approved decision (2026-09-21): the user explicitly chose session-scoped automatic
Reachy actions, without confirmation before each permitted tool use. Reason:
gestures, attention and visual interaction should work naturally once enabled.
Scope: the bound Reachy agent/device/session and explicitly enabled capabilities;
not other agents, unrelated tools or blanket host-wide automatic Live dispatch.
Motion, local camera use and external snapshot analysis retain distinct grants,
expiry, revocation and edge enforcement. This is a durable requested behavior,
not a temporary workaround. Verify it with named authorized-session tests and
default/manual-denial, other-agent, disabled-capability and expired-session tests.
At the time of approval, implementation was pending; recording approval changed
no runtime or robot state. See the implementation checkpoint below.

### Shared skills assessment after the permission decision

The portable convention is one SKILL.md per skill, with YAML name/description
metadata, Markdown instructions and optional bundled resources. The current
specification recommends metadata-first discovery and on-demand loading:
https://agentskills.io/specification .

- Jimmy already has a local implementation in products/jimmy/src/jimmy/skills.py,
  prompt discovery in instructions.py, load_skill dispatch in tools.py and tests
  in tests/test_skills.py. It is product-specific, not shared framework support.
  Its simple key/value frontmatter parser is not a complete YAML implementation;
  do not claim full format compatibility or copy it unchanged into core.
- Django has AgentKnowledge and prompt/tool configuration, but these do not
  implement a portable skill catalog, full instruction/resource loading and
  version-pinned skill assignments. No shared SKILL.md loader was found in the
  inspected core/runtime paths. Repository coding-assistant skills do not
  automatically become skills available to database-backed Studio agents.
- Recommendation (not yet a requested framework implementation): a small core
  skill contract/parser/catalog with filesystem and Django storage adapters;
  immutable skill revisions assigned to agents, scoped discovery/read tools,
  and Studio import/export/assignment. Share the same resolved revisions across
  text and Live delegation. Keep metadata small and load full guidance only as
  needed; required safety limits must never depend on loading a skill.
- Skill contents and experimental allowed-tools metadata cannot grant authority,
  install dependencies or execute bundled scripts. Reading assigned guidance
  needs no additional human prompt; executing its suggested tools still follows
  host authorization, including the approved Reachy session grants.
- Reachy does not require a full skills platform before movement works. Use it
  as an initial consumer if the shared layer is approved, rather than putting
  robot-specific behavior in core. Defer marketplaces, automatic downloads and
  script execution; test YAML compatibility, resource confinement, tenant ACLs,
  version pinning, prompt budgets and permission non-escalation before rollout.

### Implementation checkpoint (2026-09-21)

The assessment above is historical. The approved shared skills slice is now
implemented: safe YAML parsing and bounded portable bundles/catalog in core;
owner-private immutable revisions, assignment CAS, reference snapshots and shared
text/Live readers in Django; Skills authoring/import/export/assignment in Studio.
No marketplace, automatic downloads or bundled-script execution is included.

Reachy now has four host motion tools, an explicit session motion toggle,
revision-pinned grants, single-use device command claims and a bounded localhost
daemon adapter behind `--controlled --motion`. The interaction skill and
preview-first `provision_reachy_actions` command live in `products/studio`.
Provisioning preserves prompts, policy, history and customized assets and never
turns on a capability. See the host readme for exact-target setup.

Focused motion checks passed: 110 host tests, 34 edge tests, and two new
cross-repository text/Live dispatch contract cases (exit 0). The latter use the
real runtime tool map, import executor, host device endpoints and edge motion
adapter, with fake motors only. Transactional isolated test fixtures allow the
async runtime's separate connections without SQLite outer-transaction locks.
No real provider calls or robot movement were part of these checks.

Subsequent user-approved local deployment applied runtime0054, installed the
declared host YAML dependency, provisioned four tools/one pinned skill and created
a motion-aware prompt version only from the untouched original seed. Original
version, model settings and history are preserved; rerun creates nothing. Robot
bridge now uses the explicit local session-motion drop-in, with voice/motion OFF.
Host DB and robot source/unit backups are documented in deploy/LOCAL.md.

Installed daemon now reports1.10.0; its source/read-only feedback contract matches
the adapter. Actual GETs exposed/fixed a Connection: close socket-lifetime bug;
36 focused fake-motor tests pass onboard, including the real parser socketpair
regression. Web and both workers were restarted, bridge stable with zero restarts,
daemon unchanged, fresh device/controller heartbeats and zero active sessions.
Four served UI assets match the built files. User voice exploration is ready.
No microphone session or movement was started during installation. Physical
motion acceptance and PostgreSQL contention remain rollout gates; camera/vision
remain a separate unimplemented slice. Unrelated network migrations were not applied.

## Code ownership and proposed paths

| Location | Responsibility |
| --- | --- |
| `clients/agent-reachy/` (new repository) | Bridge package, CLI/service entry point, SDK adapter, media queues, motion arbiter, protocol contract, simulator/fakes, tests and deployment image. Optional tiny Reachy app wrapper/local guard lives here too. |
| `clients/agent-reachy/src/agent_reachy/` | Proposed modules: `session`, `robot`, `media`, `motion`, `studio_client`, `contracts`. Keep imports light and native media dependencies optional by execution mode. |
| `products/studio/reachy/` (new host app) | Studio-specific robot enrollment/bindings, revocation hook, Live view subclasses/routes, robot control gateway, authorized tool handlers and operational checks. |
| `packages/python/agent_runtime_core/agent_runtime_core/live.py` | Only genuinely reusable Live provider/transport contracts and adapters; no Reachy, GStreamer, aiortc or Django dependency in the base core. |
| `packages/python/django_agent_runtime/voice/` | Reuse admission, delegation, worker and history. Add only generic tested extension points where the bridge needs them; no duplicated Live controller. |
| `packages/python/django_agent_studio/` | Optional reusable device/session UI later, if a second host needs it; avoid hard-coding Reachy into the shared workspace. |
| `clients/agent-frontend/packages/agent-client/` | Generic Live client helpers only if building browser voice; not required for the headless bridge. Never use `archive/agent-client`. |

Use this root plan for cross-repository coordination. When scaffolding is approved,
create an app-local `plan.md` and select the supported Reachy app template if an
installable robot app is needed. No need to fork the whole SDK or build a general
robotics framework first. Repository remote/visibility remains an owner decision.

## Delivery sequence and acceptance gates

1. **Compatibility and media spike.** Confirm robot/firmware, released SDK audio
   support, HF authorization, outbound network/ICE/TURN and OpenAI Live access.
   Exercise microphone → voice → speaker plus a fresh camera frame; keep motion
   disabled. Compare WebRTC/primary-WebSocket complexity only if the initial
   media path is blocked. Establish a measured latency/CPU/bandwidth baseline.
2. **Studio Live wiring.** Add authenticated host subclasses/routes, owner hook,
   supervised `runlivevoice` plus ordinary run workers, migrations and bounded
   sessions. Use the existing agent/history APIs. Verify prompt parity, backend
   delegation and history through the supported read API, not just a UI transcript.
3. **Robot command/observation slice.** Add single-controller leases, control
   transport and one safe animation plus `inspect_scene`. Implement telemetry and
   result correlation before claiming successful movement. End-to-end goal:
   “What am I holding?” → current snapshot → grounded answer through Reachy.
4. **Interruption and expression.** Add a small animation library, playback-driven
   movement, queue cancellation, echo tests and graceful pause/reconnect. Test
   “stop” during both voice and motion, including a late backend result.
5. **Production hardening.** Local watchdog/physical-stop checks, owner revocation,
   cross-user isolation, session budgets, metrics and rollback. Browser controls
   show mic/camera state, connected robot, agent, stop and explicit start/stop;
   closing a browser must not accidentally leave an unconsented session running.

For a Lite or unsupported cloud setup, run the same bridge on the daemon's local
computer using the Python SDK media adapter. Remote Python GStreamer support is
platform-sensitive (upstream currently documents Linux as fully supported); do
not assume a Mac remote-media path works because this checkout is on macOS.

## Security, cost and tests

- Separate Studio authorization from HF ownership; recheck both on reconnect and
  revocation. Short-lived scoped bridge grants are not general user/API tokens.
  Store no credentials in model context, ordinary session metadata, URLs or logs.
- Treat speech and camera text as untrusted observations, not device permissions.
  Keep microphone mute, camera enable and physical motion enable distinct.
- Preserve runtime/host storage, ACL, tool approval and retention defaults. Live's
  normalized-history requirement is not permission to migrate old conversations.
  If automatic movement requires changing `LIVE_TOOL_ACCESS`, seek approval and
  retain per-tool authorization; never silently turn the whole host to `auto`.
- If seeding the agent, put a previewable/idempotent routine in the owning host
  repository. Use supported writes and leave runtime storage fields unspecified.
- OpenAI currently lists GPT-Live at **$0.05/minute ($3/hour of session duration)**,
  billed per second, plus backend model/tool costs. It is not free while idle.
  Add explicit start/end, an inactivity policy, session caps and usage reporting;
  voice, vision, agent inference, compute and TURN bandwidth are separate costs.
- Unit/contract tests: bounded buffers, format conversion, stale snapshots,
  command dedupe/expiry, lease generation, cancellation, capability negotiation,
  single-controller arbitration and redacted errors. Use fake robot/provider I/O.
- Isolated integration tests: default history path, named incompatible/private
  modes, owner revocation, malformed SDP/events, duplicate delegation, provider
  failure, worker loss, migration preservation and actual supported history reads.
- Hardware/staging tests: acoustic echo, barge-in, disconnect mid-animation,
  Wi-Fi interruption, no cross-robot reconnect, TURN fallback, soak/resource bounds,
  measured p50/p95 latency and camera evidence reaching the vision provider.
  Simulation does not validate microphones, speakers, real cameras or mechanics.
- Run Python/Django tests using root `.venv/bin/python` and isolated settings;
  PostgreSQL migration/concurrency tests must use the disposable harness, not
  Studio's database. No paid provider or physical-motion test without approval.

## Validation performed for this plan

- Core `tests/test_live.py`: **166 passed**, exit 0; optional transports mocked.
- Django prompt-parity and static Live migration checks: **19 passed**, exit 0.
- Initial Django selection also included two persisted migration tests: both
  stopped at their safety fixture, requiring the private disposable PostgreSQL
  harness. They were explicitly deselected in the successful rerun, not validated.
- No application code/dependencies changed. No production API, provider session,
  robot, deployment, database migration or hardware test was exercised.

## First implementation slice — offline validated

Approved follow-up: **one agent definition, two modalities**, without a second
voice persona/definition. Implemented locally, not deployed:

- `clients/agent-reachy/`: new independent local Git repository, no commits or
  remote. Standard-library text CLI, offline demo, same-binding text/Live control
  client, bounded HTTP/JSON, safe errors, revocation-aware lifecycle primitives
  and single-slot identity/generation-pinned camera-frame buffer. No SDK/media or
  robot commands yet. No external dependencies were added.
- `products/studio/reachy/`: token-authenticated Live create/status/close views,
  deriving the agent from an existing owned executable Studio thread. Current
  user, token HMAC reference, project/agent access, native target and organization
  AI connection are rechecked. Runtime handles prompt/history, worker lifecycle,
  private-egress restrictions, retention and delegation. Live remains disabled by
  default; no automatic tools, history migration or ACL changes were introduced.
- Text and initial speech share prompt preparation. Task delegations create normal
  `AgentRun` rows for that same agent and conversation, with current host model
  settings and manual tool policy. The Live session pins its definition snapshot;
  a later publish applies on a new session. This is comparable agent behavior,
  not identical wording or a claim that every utterance invokes the backend.
- `tests/test_reachy_contract.py`: coordinator-owned integration test connects the
  actual client to in-process host routes. It exercises canonical writes/reads,
  token authentication, the ordinary camelCase run responses and snake_case Live
  responses, without importing host environment/database settings.

Validation, all final commands exit 0 using root `.venv/bin/python`:

- Client unittest suite: **58 passed**; offline demo also passed.
- Host Reachy + existing AI connection regressions + cross-repository contract:
  **164 passed, 81 subtests passed**, with isolated SQLite migrations enabled.
- Existing core Live tests: **166 passed**. Existing Django shared-prompt tests:
  **18 passed**. Runtime implementation did not need modification for this slice.
- The first broad host run used `--nomigrations`; 34 existing verifier fixtures
  correctly rejected the absent migration records. The corrected isolated run
  with migrations enabled passed; no production setting or application workaround.
- Prior disposable-PostgreSQL upgrade/concurrency gates remain unvalidated. No
  production API, provider call, actual worker process, deployment, hardware or
  physical motion was exercised. SQLite does not prove PostgreSQL locking.

To repeat the cross-repository checks from this coordinator root:

<augment_code_snippet mode="EXCERPT">
````sh
PYTHONPATH=packages/python:products/studio:clients/agent-reachy/src .venv/bin/python -m pytest tests/test_reachy_contract.py products/studio/tests_reachy products/studio/tests_ai_connections --ds=django_agent_studio.tests.settings -q
````
</augment_code_snippet>

The development token route is explicitly **not device enrollment**: broad user
credentials stay on an approved operator machine, not the robot. Replace this
development path with short-lived scoped grants before unattended deployment.
Library Live creation has a separate 120-second client budget for provider setup
and worker readiness; a lost response remains an unknown outcome, not permission
to retry. Runtime worker cleanup/deadlines remain required.

Next: confirm firmware/address and approve SDK/WebRTC dependencies, then
implement/test microphone → Live → speaker and a fresh camera snapshot with motion
disabled. Real echo/barge-in, TURN/reconnect, consent, scoped grants and local
safety are later acceptance gates—not implied by this foundation.

## Questions to settle before hardware/deployment work

### Local startup checkpoint

- Local web process is serving `http://127.0.0.1:8001/` using
  `agent_studio.settings.dev` and the host's own Python 3.11 environment. Repaired
  seven editable installs that still pointed at pre-move repository paths; added
  the locally vendored ACE packages already required by the host. No dependency
  manifests, storage defaults or remote deployments changed.
- System checks pass. Login GET is 200, anonymous Studio GET redirects to login,
  and anonymous Reachy Live access is 401. Widget assets are served from current
  source. This is a web smoke test, not authenticated agent-creation acceptance.
- With explicit user approval, backed up the local Docker-backed PostgreSQL
  `agent_studio` database at `127.0.0.1:5432` and applied all **28 pending
  migrations**, spanning runtime/Live, Studio, ACE, MFA and SDLC delivery.
  **Zero migrations remain.** The private, mode-0600 custom-format backup is at
  `/Users/chris/Library/Application Support/AgentStudio/backups/local-agent_studio-v2338h6j/agent_studio.dump`;
  a full archive read with `pg_restore` succeeded (not a restore rehearsal).
- Post-migration `check` and `migrate --check` pass. Read-only ORM checks for
  agent definitions, projects, threads and MFA authenticators pass; existing
  business-table row counts and user password/privilege fields are unchanged.
  Login, Studio redirect, Reachy authentication and builder asset HTTP checks
  still pass. Authenticated agent creation remains for the user to verify.
- Installed the already-declared Pillow dependency, matching the host's 11.3.0,
  in the root management environment to resolve its `fields.E210` check errors.
  No dependency manifests changed. The existing local superuser account was
  preserved; password reset was not approved or performed.
- Existing local Redis is available. No run worker or Live worker was started,
  no queued jobs consumed, and no agent definition seeded. Live remains disabled
  and its key is absent; organization connection encryption is also unconfigured.
  Provision appropriate local credentials securely before inference tests.
- The schema is ready for the user to sign in and create the Reachy agent.
  At the user's explicit request, local dev settings now exempt only their
  existing account (`chris.barry@makemoredigital.com`) from forced MFA enrollment.
  There were no enrolled authenticators to remove; password and verification
  records were not changed. Production/staging and other users remain enforced.
  All 19 isolated MFA tests pass, including enrolled-authenticator login checks;
  the local account's enforcement decision and Django checks were verified
  read-only. See the host README for scope, tests and removal of the exception.

### Local agent and robot checkpoint (2026-09-20)

- User authorized local agent setup and necessary robot SSH installs. SSH as
  `pollen` works at `100.119.90.71`; daemon 1.9.0 is active, HTTP port 8000 and
  WebSocket port 8443 respond. Local signaling returned a `reachymini` producer.
  The daemon venv `/venvs/mini_daemon` has GStreamer/numpy/websockets but no
  aiortc/av or newer `central_consumer.py`. No installs, daemon restarts, motion,
  microphone/camera capture or speaker playback were performed.
- Added reusable host `provision_reachy` command/service: explicit owner/org,
  read-only preview, guarded local apply, supported native creation/activation,
  project and thread writes, no storage/ACL/model overrides. Reruns preserve
  existing state; ambiguous/partial identities fail closed, not auto-repair.
- Previewed then applied only to the approved local owner in their sole managed
  organization, **Demo workspace** (`1fa086f6-f296-4b5c-9422-fda15e53d1af`).
  Published **Reachy**, slug `reachy-local`:
  - Agent: `6ec5e805-dc45-5241-b9ab-88de8bfaa750`.
  - Project: `6d1f32b7-8513-4bef-aece-f05a62e0c9dd`.
  - Thread: `0bf5ed30-80a6-4173-b608-467703740fed`.
  - Conversation: `6e9f28a9-a97f-45a0-b709-767a6907e7a0`.
- Readback: agent storage override blank; conversation initialized normalized;
  zero messages/runs. Owner-authorized in-process Studio agent/thread/history
  API reads returned 200. Rerun under an SQL write blocker reused all records
  without writes. These are not live browser/authentication or inference tests.
- Validation: **72 provisioning tests**; combined Reachy/AI connection/client
  contract **236 passed, 81 subtests passed**, exit 0 in isolated SQLite.
  Fixed guard-test simulations to mock command settings only (Django otherwise
  imports the fake router; nested settings holders also shadow SETTINGS_MODULE).
  PostgreSQL concurrency remains a separate unverified gate.
- Credential check: local organization AI connection absent, Fernet encryption
  key absent, Live disabled and Live key absent. Do not reuse production secrets,
  start queue consumers or claim text/voice inference works. Next obtain approval
  for stable local encryption setup and developer-machine media dependencies;
  user enters organization credentials via supported UI and Live key privately.
- No voice start/stop CLI/UI or wake detector is implemented. Begin with explicit
  Start/Stop, bounded queues and teardown, no motion. Later opt-in local "Hey
  Reachy" detection can open sessions without idle cloud audio; an end phrase
  and inactivity close need their own implementation and acceptance tests.

1. Confirm local credential provisioning and media dependency installation scope.
2. Prefer headless cloud operation (recommended), or a browser-open prototype first?
3. Which remote and visibility should the new local `clients/agent-reachy/` repository use?
4. Which published Studio agent should own Reachy, and who may enable its motion?
5. Initial consent/session budget: explicit start/stop, or an approved idle/wake policy?

## Upstream sources

- [Reachy SDK entry point](https://github.com/pollen-robotics/reachy_mini/tree/main/docs/SDK)
- [Python SDK](https://github.com/pollen-robotics/reachy_mini/blob/main/docs/source/SDK/python-sdk.md)
- [Media architecture](https://github.com/pollen-robotics/reachy_mini/blob/main/docs/source/SDK/media-architecture.md)
- [Cloud consumer guide](https://github.com/pollen-robotics/reachy_mini/blob/main/docs/source/SDK/cloud-backend-consumer.md)
- [Cloud consumer source](https://github.com/pollen-robotics/reachy_mini/blob/main/src/reachy_mini/media/central_consumer.py)
- [JavaScript/media/animation reference](https://github.com/pollen-robotics/reachy_mini/blob/main/docs/source/SDK/javascript-sdk.md)
- [GPT-Live overview](https://developers.openai.com/api/docs/guides/live)
- [GPT-Live model, modalities and pricing](https://developers.openai.com/api/docs/models/gpt-live-1)
- [Primary WebSocket audio](https://developers.openai.com/api/docs/guides/voice-websockets)

Sources above track moving upstream versions. Recheck them when pinning dependencies.
