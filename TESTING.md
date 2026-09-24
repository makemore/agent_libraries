# Testing policy: run tests in local Docker containers

Applies to human contributors and coding agents, for any project, language or
delivery workflow. It adds to any project-specific engineering rules; it does
not replace them.

## The rule

Run automated tests in **local Docker containers, in the background**. Do not
run test suites in the foreground on the host.

## Why

- **The agent gets back to the user sooner.** A foreground test run blocks the
  agent until it finishes. With the tests running in a container, the agent can
  report back straight away, ask the user to check things by hand, and pick up
  the automated results when they are ready.
- **Many test runs can happen at once.** Each container is isolated, so
  different packages, test files or variants (SQLite and Postgres, for example)
  can run side by side without fighting over ports, databases, caches or files.
- **The host stays clean.** Containers never touch the host database, the
  user's running services or the user's environment.

## Workflow for agents

1. Make the change and save it.
2. Start the relevant tests in a local container as a **background** process
   (for example `docker compose run --rm ...` or `docker run --rm ...` started
   in the background). Give each run a unique container or project name so
   parallel runs don't clash.
3. **Return to the user right away.** Say what changed, what is being tested in
   the background, and what they can check by hand in the meantime (exact
   steps, URLs or commands to try).
4. When the background run finishes, read its output and report the result
   honestly: passed, failed (with the failing tests) or could not run (and why).
5. If tests fail, fix the problem and start a new background run. Don't wait for
   the user to ask.

Split slow suites into several containers (by package or test file) and run
them in parallel instead of one long run.

## Container requirements

- **Local only.** Build and run images on this machine. Don't push test images
  or run tests against shared or remote infrastructure.
- **Isolated settings.** Use each package's test settings and a throwaway
  database inside the container (or a sibling container such as Postgres).
  Never point tests at the host database or a real service.
- **No secrets.** Don't bake credentials into images, Dockerfiles, compose files,
  build arguments or logs. Tests must run offline with fakes or stubs unless a
  test is explicitly an integration test the user asked for.
- **Mount the source read-only where possible**, so a test run can't modify the
  working tree. Write caches and artifacts to container-local paths or named
  volumes.
- **Clean up.** Use `--rm` (or `docker compose down`) so finished containers and
  their temporary databases don't pile up.
- **Same defaults as the product.** Don't override product settings or
  defaults in the container setup or shared fixtures just to make tests pass.

## Reusable setup

Keep the Dockerfile or compose file for each project's tests in the owning
repository, next to the tests, so any contributor or agent can start the same
run. Don't leave the only copy of a test command in terminal history. Add a
short note (or a script or build target) showing how to start it.

## Manual testing by the user

Automated container runs don't replace manual checks for things tests can't
cover well, such as UI behaviour, editor integrations and real devices. While
the containers run, the agent should give the user a short, concrete list of
what to try and what they should see, then combine the user's findings with the
automated results in its final report.

## Exceptions

Some tests can't run in a Linux container, for example macOS or iOS tests, or
tests that need host hardware. Run those on the host, still in the background
where possible, and say in the report that they were run outside Docker and why.

If a project has no container setup yet, say so, and don't quietly fall back to
the host. Add the setup, or ask whether to run on the host this once.
