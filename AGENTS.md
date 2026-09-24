# Development defaults and setup policy

Applies to work in this meta-repo and its checked-out packages. Package-specific
guidance also applies. This policy is for human contributors and coding agents.

## Defaults are part of the product contract

- General demos, seeds, examples and fixtures must inherit the supported runtime
  and host defaults. Omit optional settings instead of copying today's defaults
  into persistent records or configuration.
- Do not change storage, authorization, sharing, retention, validation, queues or
  other product semantics merely to make a UI demo or smoke test easier to pass.
  Resolve the integration gap, or report the blocker and request a decision.
- Use supported creation/write services. A screen displaying synthetic data is
  not evidence that the production write/read path works.
- Preserve deliberate host overrides and existing persisted choices. Adopting a
  new default for new objects is separate from migrating existing data. Privacy
  restrictions must still be enforced; compatibility must not weaken them.

## Exceptions must be explicit

- A compatibility, failure-mode or privacy-specific test may override a default.
  Name that scenario and keep the override local; never hide it in a general
  shared fixture or factory.
- Record the reason, scope, behavioural impact and verifying test next to any
  non-default setup. For a temporary workaround, include its removal criterion.
- Before a durable demo/host override, get explicit approval for that behaviour.
  Permission to populate a demo is not permission to change its storage or ACLs.
  Do not silently reset existing overrides during a seed rerun.

## Repeatable setup and verification

- Before populating a persistent demo, put the reusable seed routine in the
  owning repository. Do not leave its only implementation in terminal history.
- Provide a read-only preview, explicit targets, idempotent creation and an
  isolated test. Reruns must not overwrite user-owned configuration/history.
- Keep secrets out of scripts, fixtures, arguments and output. Summaries should
  contain only safe identifiers, configuration choices and counts.
- Add and run default-path tests as well as named exception tests. Verify the
  stored state and supported read API, not only rendered output or HTTP status.
- Run automated tests in local Docker containers in the background, as described
  in [TESTING.md](TESTING.md). Use isolated test settings, never the host
  database.

## Conversation history example

- Leave `AgentDefinition.message_storage_mode` blank and new conversation
  `history_*` fields to runtime initialization. Do not force either JSON or
  normalized mode in a general factory; respect the host setting.
- The current runtime defaults to normalized history for new conversations and
  disables lazy legacy conversion. Explicit JSON compatibility remains supported.
- Synthetic normalized turns must use the runtime's history writer under its
  lock/transaction contract, not only insert `AgentRun.input/output` JSON.
- Studio workspace-specific checks and seeding rules are in
  [WORKSPACE.md](packages/python/django_agent_studio/WORKSPACE.md#fixture-and-demo-policy).