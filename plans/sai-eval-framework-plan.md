# S'Ai Eval Framework + Onboarding Smoke Test — Plan

Status: **DRAFT / awaiting sign-off**. Tracks the generic test/eval product being
added to `django_agent_studio` and the Resilient Minds (iOS) onboarding smoke test,
plus every S'Ai definition change that must ship to prod.

## 1. Goal

1. A **generic** eval product in `packages/python/django_agent_studio` that can:
   - run 200+ test cases against an agent **or** a multi-agent system;
   - drive multi-turn conversations where the "user" side is an LLM (LLM-as-user);
   - evaluate outcomes (LLM-as-judge + deterministic assertions);
   - emit a review report + **suggested changes** to the agent definition;
   - apply approved changes to Studio agents after human sign-off, or auto-apply
     in "auto mode" — every applied change logged for prod deployment.
2. An iOS **"S'Ai onboarding smoke test"** sidebar item in Resilient Minds that
   walks the full onboarding flow on-screen with an LLM-driven simulated user,
   logs each turn to the Xcode console, and is manually triggerable by a user.

## 2. Architecture findings (grounding)

- **Definitions** (`django_agent_runtime/models/definitions.py`): `AgentDefinition`
  → active `AgentVersion` (`system_prompt`, `model`, `model_settings`, `tools`),
  `AgentRevision` (save snapshots), `SpecDocument`/`SpecDocumentVersion`,
  `AgentTool`. `AgentSystem` for multi-agent.
- **Execution**: a run is a row created by `POST /runs/` (`BaseAgentRunViewSet`):
  body carries `agent_key`, `messages`, `params`, optional `conversation`,
  `idempotency_key`, ephemeral `memories`. Run is dispatched to the task backend /
  Redis stream; `AgentRunner.run_once` (`runtime/runner.py`) executes it via the
  registry (the DB-backed **dynamic** agent loads config from the definition).
- **Streaming**: SSE `/runs/<id>/stream/` (async, event bus) or
  `/runs/<id>/events/` (sync polling). Events include `assistant.message`,
  `tool.call`, `run.succeeded|failed|cancelled|timed_out|suspended`.
- **Studio API** (`django_agent_studio/api/urls.py`): agent CRUD, `versions/`,
  `revisions/`, `save/`, `publish/`, `spec/`, systems. Studio `models/` currently
  holds only `permissions.py`, so the eval app adds new models here.
- **Host project**: `products/studio` (Django project, `manage.py`, sqlite dev
  db, `requirements.txt`). Management commands live under each app's
  `management/commands`.
- **iOS intro** (`resilient/ios/.../AppViewModel.swift`): `replayIntro()` /
  `injectIntro()` stream an assistant welcome via
  `appendAssistantMessageStreamed(..., speak:)` then append an `actionButtons`
  block; button taps fire real user turns through the library's `onBlockAction`.
  Sidebar item lives in `AppSidebarView.swift` ("Replay S'Ai intro").

## 3. Proposed design — Studio eval app

New code under `packages/python/django_agent_studio` (namespaced, generic — not S'Ai-specific):

- **Models** (`models/evals.py`):
  - `EvalSuite` — name, target (agent_key **or** system), default judge model.
  - `EvalCase` — belongs to suite; `persona`/`user_goal` (drives LLM-as-user),
    `seed_messages`, `rubric`, deterministic `assertions`, `max_turns`, tags.
  - `EvalRun` — a batch execution of a suite against a target version; status,
    aggregate scores, config snapshot.
  - `EvalCaseResult` — per-case transcript, metrics, pass/fail, judge scores.
  - `SuggestedChange` — proposed edit (`field`=system_prompt|spec|model|
    model_settings|tool, before/after, rationale, linked results); state =
    proposed→approved→applied / rejected; `applied_version_id`, `applied_at`.
- **Services** (`services/evals/`):
  - `simulated_user.py` — LLM-as-user turn generator from persona + goal +
    transcript so far; stop signal when goal met/abandoned.
  - `runner.py` — for each case, loop: user turn → create run against target →
    consume SSE → collect assistant reply + tool calls → repeat to `max_turns`.
    Bounded concurrency for 200+ cases; structured console logging throughout.
  - `judge.py` — LLM-as-judge over transcript+rubric, plus assertion checks.
  - `report.py` — markdown + JSON report; aggregates + failure clustering.
  - `suggestions.py` — turn recurring failures into `SuggestedChange` rows;
    `apply()` writes via Studio `save/`+`publish/` (or spec endpoint); auto mode
    applies immediately, otherwise waits for approval. Always appends to §5 log.
- **API** (`api/` additions): suites/cases CRUD, `suites/<id>/run/`,
  `eval-runs/<id>/` (results+report), `suggested-changes/<id>/review/`.
- **Management command**: `manage.py run_eval_suite <suite> [--auto-apply]` for
  headless/CI runs.

## 4. Proposed design — iOS onboarding smoke test

- New sidebar item "S'Ai onboarding smoke test" in `AppSidebarView.swift` →
  `appViewModel.startOnboardingSmokeTest()`.
- A client-side `OnboardingSmokeTestRunner` drives the **real** chat UI so the
  walkthrough is visible: it resets/opens a fresh conversation, lets S'Ai speak,
  then asks an LLM-as-user (a dedicated Studio "user simulator" agent, or a direct
  model call) for the next user message based on S'Ai's last reply, injects it as
  a real user turn, and loops until the onboarding goal is reached or `maxTurns`.
- Verbose `print("[SmokeTest] …")` logging per turn (user msg, S'Ai reply, tool
  calls, timing) so it is tunable from the console.
- Server-side eval framework (§3) runs the same scenario headless at scale; the
  iOS button is the human-visible single-run path.

## 5. S'Ai definition change log (fill as we go — prod deployment gate)

| Date | Field (prompt/spec/model/tool) | Change | Source (eval run / manual) | Status |
|------|--------------------------------|--------|----------------------------|--------|
| _tbd_ | _tbd_ | _tbd_ | _tbd_ | _tbd_ |

> Any row here = a Studio agent change that must be published to the prod S'Ai
> agent. Capture the new `AgentVersion`/revision id once applied.

## 6. Prod deployment checklist

- [ ] New migrations for the eval app applied (`agent_studio` project).
- [ ] Eval app added to `INSTALLED_APPS` / URLs wired in host project.
- [ ] Judge + user-simulator model keys present in prod env.
- [ ] S'Ai definition changes from §5 published to prod agent + verified.
- [ ] iOS smoke-test item gated to non-prod / debug builds if desired.
- [ ] Eval API auth/permissions reviewed (Studio permission model).

## 7. Open questions (need answers before build)

1. **Target binding**: tune **S'Ai as a single agent**, or as a multi-agent
   **system**? Which `agent_key` / system identifies S'Ai in your Studio db?
2. **Execution path for evals**: call the runtime in-process (create `AgentRun`
   rows + read events) vs. HTTP against a running server. In-process is simpler
   for CI; HTTP exercises the real stack. Preference?
3. **Suggested-change surface**: edit the **system prompt**, the **SpecDocument**,
   model/settings, tools — or all? S'Ai's behaviour: prompt-driven or spec-driven?
4. **Auto mode default**: should auto-apply be off by default (propose-only) with
   sign-off required, and auto reserved for an explicit flag?
5. **iOS user-sim source**: dedicated Studio "user simulator" agent (reusable in
   §3 too) vs. a direct model call from the app? Any model/cost constraints?
6. **Scope now**: build the full Studio framework first, or land the iOS smoke
   test (immediate, visible value) against the existing S'Ai, then generalise?
