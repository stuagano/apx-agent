# Capability contracts for apx-agent

**Date:** 2026-09-05  
**Status:** approved design  
**Scope:** make Ctk/caps the load-bearing proof layer for apx-agent's public
runtime promises, with cheap CI proofs and explicit live Databricks proofs.

## Problem

apx-agent already vendors Ctk, caps, 44 `*_reality_ctk.py` files, a
`capabilities.yaml`, and a Claude Stop hook. The manifest declares no
capabilities, so `caps verify` is vacuously green. The caps framework itself has
no repository tests, its pytest runner does not use the `python/` environment,
and its hook does not enforce anything in Cursor.

This allowed green local and CI runs to coexist with repeated deployed-path
repairs. The September eval/MLflow chain is the clearest example: tests proved
the local `evals.json` fallback while deployed Apps read MLflow assessments and
exposed identity, payload-shape, cold-start, retry, and cache-warming defects.

## Goals

- Declare every load-bearing, user-visible apx-agent promise once.
- Prove framework behavior cheaply on every change.
- Prove environment behavior against a deliberately selected Databricks
  reference deployment.
- Invalidate proofs when relevant implementation or check files change.
- Prevent a skipped, missing, stale, or misconfigured check from becoming
  "proven."
- Give Claude and Cursor useful in-band feedback while keeping CI/release gates
  authoritative.
- Preserve waivers and unrelated local work.

## Non-goals

- Mirror all ~139 reality-test nodes in `capabilities.yaml`.
- Treat every internal invariant as a public capability.
- Auto-select a Databricks CLI profile.
- Run live workspace checks in pull-request CI.
- Auto-revert files after a failed proof.
- Make the static app-readiness audit apply patches.

## Design principles

1. **Caps are product promises.** Tests are evidence behind a promise.
2. **Cheap and live proofs are separate entries.** A framework failure must not
   look like a network or workspace failure.
3. **No skipped proof.** Missing live configuration is an error/unproven state,
   never a pass.
4. **Narrow invalidation.** Each cap lists only source and check paths that can
   invalidate the promise.
5. **One canonical environment.** apx-agent checks run through the locked
   `python/` uv environment.
6. **Explicit identity.** Live checks require `APX_CAPS_PROFILE`; ambient
   `DATABRICKS_CONFIG_PROFILE` is not accepted as proof configuration.
7. **Runtime readiness supplies the contract catalog.** The
   `app-production-readiness` checks identify what an Apps capability must
   prove; caps records and executes the proof.

## Architecture

### Layer 1: prove caps itself

Before populating the manifest, add repository tests for:

- valid, empty, and malformed manifests;
- all-skipped pytest checks becoming `error`, not `pass`;
- cheap proof becoming `code-stale` after a dependency changes;
- active waivers surviving full and concurrent verification;
- explicit single-cap verification overriding a waiver;
- Claude gate block output and recursive-stop protection;
- project-local hook discovery;
- the locked `python/` test execution path;
- tier filtering for `verify` and `status --check`.

The runner must support an apx-agent check command rooted in `python/`. The
smallest implementation is to use shell checks such as:

```sh
cd python && uv run --frozen pytest tests/<file>.py::<node> -q
```

`caps verify --tier cheap` runs in PR CI. `caps verify --tier live` is reserved
for the reference-deployment workflow. Bare `caps verify` remains available for
an explicit full run.

### Layer 2: enforcement policy

- **Cheap caps:** block completion when never proven, failed, errored, or
  code-stale. They run in CI on every pull request.
- **Live caps:** run after merge and on a schedule. Never-proven, failed, or
  expired live caps block release promotion, not ordinary local edits.
- **Waivers:** require a reason and expiry, remain visible in status, and are
  preserved by ordinary verification. They do not turn a failed proof into a
  pass.
- **Claude:** the project Stop hook reports blocking cheap caps.
- **Cursor:** commit `.cursor/hooks.json` using Cursor hook schema `version: 1`
  and a `stop` command with a bounded `loop_limit`. The adapter resolves the
  first workspace containing `capabilities.yaml` from `workspace_roots`, then
  `CURSOR_PROJECT_DIR`, `cwd`, and `transcript_path`. On a completed turn with
  blocking cheap caps it emits `{"followup_message":"..."}`; on success, abort,
  error, or an exhausted loop budget it emits `{}`. Cursor `stop` cannot veto
  completion, so this is corrective feedback rather than a hard lock.
- **CI:** runs the caps framework tests and `caps verify --tier cheap`.
- **Release/live workflow:** runs `caps verify --tier live` with explicitly
  configured resource identifiers and `APX_CAPS_PROFILE`.

`beforeShellExecution` is not used as a completion gate: it can deny commands,
but a capability proof is a completion policy. Hook failures remain fail-open
and visible. `failClosed: true` and exit code 2 do not turn Cursor `stop` into a
hard completion veto.

### Layer 3: cheap capability inventory

Each entry is `tier: cheap`, defaults to code freshness, and runs one focused
shell command that may include multiple test nodes when they jointly prove one
promise.

1. **`apps-scaffold-host-wiring`**  
   Given an Apps-target agent declaration, when scaffold/generate writes the
   project, then the non-empty Python/AppKit host artifacts import the declared
   agent and expose the serving bridge.

2. **`served-runtime-readiness`**  
   Given a declared agent, when `create_app` starts, then `/invocations`,
   `/responses`, A2A discovery, and `/readyz` are mounted once, and readiness
   returns 503 rather than a false ready or 500 when a required component is
   degraded.

3. **`apps-identity-and-user-scoping`**  
   Given an Apps request, when caller identity is absent or spoofed, then
   governed operations fail closed; when two trusted principals use the same
   raw session identifier, their session, memory, and cache keys remain
   isolated.

4. **`a2a-delegation-and-identity`**  
   Given `sub_agents=[url]`, when the root delegates across two hops, then the
   declared peer actually executes and the leaf receives the original trusted
   caller identity.

5. **`governed-tool-approval`**  
   Given a denied or approval-required tool, when it is invoked through served
   Chat/Responses/composite paths, then denial blocks execution and
   approve/deny/resume produces the audited result.

6. **`sql-terminal-state-and-cancellation`**  
   Given Statement Execution returns RUNNING after its synchronous wait, when
   apx-agent polls or the caller cancels, then success returns rows, failure
   surfaces the actual error, timeout remains distinct, and cancellation reaches
   the statement.

7. **`mlflow-trace-and-eval-read`**  
   Given a real local MLflow experiment containing a trace and assessment, when
   trace/eval APIs and the dev UI query it, then the trace and eval case are
   returned using the production MLflow data shape. `evals.json` proves only its
   documented local fallback and cannot satisfy this cap.

8. **`trace-feedback-identity-roundtrip`**  
   Given a deployed-style request with trusted OBO identity, when feedback is
   written and read, then MLflow receives the right credential path and the
   reviewer/source and assessment value round-trip. Missing identity fails
   closed.

9. **`durable-session-and-lakebase-wiring`**  
   Given declared durable sessions/memory, when the app starts and stops, then
   the Lakebase checkpointer/store is selected, turn two recalls turn one, setup
   failure closes pools, and lifespan shutdown disposes engines.

10. **`serving-protocol-and-concurrency`**  
    Given Chat and Responses serving surfaces, when requests stream or overlap,
    then protocol events complete in order, visible assistant text is
    normalized, and one request does not block the event loop.

11. **`observability-trace-roundtrip`**  
    Given an instrumented agent run, when a trace is written, then the dev UI
    lists it, trace details retain events, and cross-agent spans retain a common
    correlation tag.

12. **`deploy-artifact-and-gate-freshness`**  
    Given a deployable project, when deploy runs, then stale pins or degraded
    readiness abort, generated lock/host artifacts correspond to the current
    source, and successful state records the deployed target.

### Layer 4: live capability inventory

Live checks are scripts under `checks/`. They use explicit environment
configuration, return exit 3 for unreachable/missing infrastructure, return
non-zero for a contract failure, and print only sanitized evidence.

1. **`live-app-boot-readyz`**  
   A selected deployed App is RUNNING and authenticated `/readyz` returns
   `status=ready`.

2. **`live-app-user-isolation-a2a`**  
   Two configured test users cannot read each other's state, and a configured
   A2A chain attributes the leaf operation to the originating user.

3. **`live-sql-cold-terminal-state`**  
   A cold configured SQL warehouse starts, a representative statement reaches
   a terminal state, and timeout/cancellation cannot become a false empty
   result.

4. **`live-mlflow-eval-feedback-roundtrip`**  
   A deployed agent produces a UC-backed trace; the eval UI reads its
   assessment; OBO feedback writes and reads the same trace as the selected
   user.

5. **`live-lakebase-restart-durability`**  
   A configured Lakebase-backed conversation/approval is written, the serving
   process is restarted, and the same scoped state reads back.

6. **`live-appkit-runtime-parity`**  
   A selected AppKit-hosted deployment answers the representative Chat,
   Responses, bridge-tool, and readiness probes, and its recorded source SHA
   matches the intended build.

7. **`live-grounded-platform-tools-obo`**  
   Configured Genie and Knowledge Assistant resources return non-empty grounded
   results as the selected user; the answer includes structured rows or source
   citations as appropriate.

## Capability-to-readiness mapping

- **Performance/scalability:** serving concurrency, SQL cold path, AppKit
  runtime parity.
- **Resilience:** readiness degradation, external timeouts, terminal-state
  polling, cancellation, pool cleanup, restart durability.
- **Observability:** trace write/read, correlation, deployed readiness evidence.
- **Security:** fail-closed identity, per-user state, governed approvals, OBO
  feedback and platform tools.
- **Accessibility:** not promoted to a framework cap until apx-agent declares a
  stable customer-facing UI accessibility contract.
- **AI/LLM cost:** not promoted until apx-agent exposes a measurable budget or
  routing contract. Existing guidance alone is not a capability.

This exclusion is deliberate: "all caps" means all promises the product can
currently state and prove, not aspirational checks with no contract.

## Data and configuration

No credentials, tokens, hostnames, or resource IDs are committed. Live scripts
read explicit variables:

- `APX_CAPS_PROFILE`
- `APX_CAPS_APP_URL`
- resource-specific IDs only for the selected live checks

The live workflow supplies these from protected configuration. Scripts reject
ambient profile selection and redact authorization material from output and
the caps ledger.

## Failure handling

- Check assertion failure: `fail`, with bounded diagnostic output.
- Missing dependency, resource, or authentication: `error`, never `pass`.
- Expired live proof: visible and release-blocking, but not a local Stop loop.
- Active waiver: visible with reason and expiry; ordinary verify preserves it.
- Two failed implementation hypotheses for the same cap: stop editing, retain
  the diff, and run the relevant app-readiness domain review before another
  implementation attempt.

## Delivery sequence

1. Add caps framework tests and tier selection.
2. Align runner/hook/doctor behavior with the repository and Cursor.
3. Add the 12 cheap entries using existing proofs.
4. Write the missing SQL cancellation and MLflow eval production-path tests.
5. Prove all cheap caps and require them in CI.
6. Add the seven live entries and explicit probe scripts.
7. Configure the reference-deployment workflow without selecting or deploying
   to a workspace in this change.
8. Run the full repository gate and read back manifest, ledger, and CI wiring.

## Acceptance criteria

- `capabilities.yaml` declares the 12 cheap and seven live promises above.
- `caps doctor` validates every entry and project-local enforcement wiring.
- Cursor project hooks use schema version 1, resolve native workspace fields,
  and emit bounded `followup_message` feedback only on completed turns.
- Caps framework tests prove waiver, freshness, skip, tier, and gate behavior.
- `caps verify --tier cheap` passes in the locked repository environment.
- Missing live configuration produces unproven/error, not a passing cap.
- Live caps never run in pull-request CI.
- The MLflow eval cap fails if the implementation falls back to
  `evals.json` while an experiment is configured.
- The SQL cap fails if RUNNING is treated as empty success or cancellation is
  not forwarded.
- No existing user changes, generated build output, local profiles, or
  credentials enter the branch.

