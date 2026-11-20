# Approval continuation after worker replacement (apx-ab7)

**Date:** 2026-11-20 · **Status:** BLOCKED — deployed acceptance NOT RUN ·
**Issue:** [#864](https://github.com/stuagano/apx-agent/issues/864) ·
**Boundary:** [durable host consumers](durable-host-consumers.md#live-864-acceptance-ledger-apx-fgj)

This is a blocked-verification record and operator handoff, **not a successful
live run record**. No App was provisioned, deployed, invoked, faulted, inspected,
or torn down for this work. No live principals, session/invocation IDs, worker
IDs, store records, effects, or audit records were collected. The bead stays
open; local green gates do not satisfy its deployed acceptance.

## Blocker and authority boundary

The operator has not supplied an authorized disposable deployed App, permission
for worker fault injection, store/effect/audit access and teardown, or two
distinct OAuth principals. The assignment forbids deployment. Before a live run,
obtain explicit authorization for each of those operations and securely
configured Apps U2M/M2M OAuth contexts for the authorized and unauthorized
principals. An App service identity used for infrastructure/storage is not a
substitute for the caller's authority. Never use PAT or ambient App credentials
to fill a missing user identity; never persist tokens in evidence or checkpoints.

Do not retry parked `agent-bricks-stu-dur-1002`. The old
`agent-bricks-864probe` and its stores were deleted on 2026-10-21. Neither is an
available disposable probe. If a new deployment is necessary, the operator must
first explicitly change the no-deploy instruction.

## Exact runtime restriction and fresh continuation path

The frozen environment for this investigation uses `databricks-agentbricks`
**0.10.0** (which supplies the `databricks_agentkit` import),
`databricks-sdk` **0.146.0**, and `langgraph` **1.1.10**, as pinned in
[`python/uv.lock`](../../python/uv.lock). Source inspection was against base
revision `0c15c9f8` plus the local regression test linked below.

- In the installed SDK, `databricks_agentkit/runtime/app.py`,
  `DurableAgentServer._execute` rejects a request-user recovery attempt or a
  missing in-memory request auth context with `AuthError` code
  **`MCP_USER_AUTH_RECOVERY_UNSUPPORTED`**, HTTP status **500**, message
  **`Request-user execution does not survive failure recovery yet`**, before
  invoking agent code.
- `databricks_agentkit/runtime/auth.py`, `RequestAuthContext` cannot be
  serialized; `close()` drops its token and clients. Credentials are transient,
  not reconstructed from the Runtime Store or Session Store. Polling or replay
  cannot restore them. SDK `namespace()` scopes both session and invocation IDs
  by host/workspace/App/principal, in addition to APX's checkpoint scoping.
- APX [`_durable_agent.py`](../../python/src/apx_agent/_durable_agent.py),
  `build_native_manifest` and `compile_durable_handlers.request_auth`, rejects
  request-user `recovery=True` with **`Request-user recovery is unsupported`**.
  `request_principal` resolves the authenticated principal afresh from the SDK
  user client; `session_thread_config` keys checkpoint state by principal and
  session. Do not remove these guards to make a probe pass.
- For a **completed invocation whose agent output is interrupted for approval**,
  the candidate supported path is a **new authenticated invocation**, with a
  **new UUID** and the **same top-level caller session ID**, carrying
  `input: {"resume": "approve"}`. Use request-user auth with automatic recovery
  disabled. This is ordinary invoke dispatch (`is_recovery=false`), not a retry
  of the original invocation or credential reconstruction. APX's direct governed
  graph handler accepts this path locally; it remains unverified after deployed
  replacement. An invocation still active/abandoned at worker loss is a different
  case and is not made recoverable by this procedure.

The SDK's shipped `runtime/ARCHITECTURE.md` also describes generated
LangGraph/OpenAI adapter OBO HITL/resume as unsupported
(`MCP_USER_AUTH_HITL_UNSUPPORTED`). That adapter statement is **not evidence that
APX's direct governed handler rejects fresh authenticated resume**. Do not switch
adapters, identity mode, or enable automatic request-user recovery as a workaround.
No upstream blocker for the fresh APX path has been established by a live run.
If that path fails, preserve the actual version, API, sanitized response/error,
checkpoint and effect readback, and report the precise required upstream support;
leave apx-ab7 open. Automatic request-user recovery remains blocked as above.

## Local evidence (not worker replacement or OAuth proof)

[`test_native_approval_fresh_auth_and_duplicate_resume`](../../python/tests/test_native_invocation_contract.py)
uses the real SDK HTTP surface with synthetic trusted-ingress headers, a mocked
user client and scripted model, `InMemoryRuntimeStore`, and `InMemorySaver`.
There is no real OAuth edge, durable remote storage, process death, replacement
worker, or independent audit backend in this test.

It checks:

1. The original invocation completes at the transport layer with
   `output.status="interrupted"` and zero effects. Independent saver readback
   contains a pending `__interrupt__` write; transient auth is discarded.
2. A different principal cannot read the original invocation (HTTP 404).
   Resume under the same textual session and resume invocation IDs enters that
   principal's separate namespace. With no pending work there it returns
   HTTP 500 / `AGENT_ERROR`, produces no effect, and leaves the owner's saved
   checkpoint unchanged. **This is isolation, not an HTTP 403 authorization
   response or proof of a deployed authorization policy.**
3. A fresh authenticated owner request with a new invocation UUID in the same
   caller session completes, produces one effect under the owner principal,
   and advances the saved checkpoint without a pending interrupt.
4. Repeating the same resume invocation ID returns its persisted result.
   A second new resume UUID also completes without repeating the tool effect.
   Transport idempotency and consumed-approval behavior are checked separately.
   This is not a universal exactly-once guarantee for external effects.

Reproduce locally:

```sh
cd python
uv run --frozen pytest -q tests/test_native_invocation_contract.py tests/test_durable_recovery.py
```

The existing service-identity
[`test_recovery_preserves_approval_and_resume`](../../python/tests/test_durable_recovery.py)
reconstructs handlers and SDK savers against a local REST fake. It is separate
coverage, not cross-principal authorization or deployed replacement proof.

### Local gate results (2026-11-20)

- `env -u OTEL_SDK_DISABLED make check`: **6118 passed, 13 skipped**, 194 warnings.
  The skips are existing suite behavior; this change adds no skip or weakening.
  Both lockfile registry checks passed. No deployed acceptance was attempted.
- `UV_FROZEN=true uvx pre-commit run --all-files`: **all hooks passed**.
  `pre-commit` is not installed on PATH, so this uses the repository-documented
  `uvx` fallback. Without frozen mode, the OKF hook's nested `uv run` rewrote the
  lock to the machine-configured internal registry, causing hook churn. Only
  that generated lock churn was restored; frozen mode runs every hook against
  the committed dependency lock without changing hooks or skipping validation.

## Authorized operator runbook (all live steps NOT RUN)

Prepare an operator-approved, checkpointed request-user App using APX's native
handler, with automatic recovery disabled. Use a disposable independently
readable effect sink and stable business key. Do not remove policy/approval,
audit, compatibility, cancellation, or any other custom runtime machinery.
Retirement still requires consumer-specific equivalence proof.

Record the operator's scope/authorization reference, UTC run times, App/workspace
aliases, code revision, SDK versions, identity mode, relevant scope/grant results,
Runtime/Session Store bindings, and principal aliases. Keep credentials and raw
sensitive identities out of commits. Preserve the mapping securely with the
operator if needed. Capture these steps in a separate dated sanitized run record:

| Step | Required independent observations | Current result |
|---|---|---|
| Pending approval | Owner's original invocation ID and caller session ID; transport completion with interrupted output; approval/checkpoint readback; audit entry; effect count zero | NOT RUN |
| Worker loss/replacement | Authorized fault boundary **after approval persistence**, original/replacement worker IDs and mechanism, attempt IDs/numbers where exposed; readback on the replacement without resuming; approval still pending and effects zero | NOT RUN |
| Wrong principal | New resume invocation UUID, same textual caller session; original record/events access denied or isolated; actual response semantics; owner's pending checkpoint unchanged; zero effects and audit readback | NOT RUN |
| Authorized continuation | Fresh OAuth, new resume invocation UUID, same owner session; `is_recovery=false`/ordinary first attempt where exposed; saved approval consumed/completion checkpoint, one sink effect and attributable audit entry | NOT RUN |
| Same-ID retry | Reuse authorized resume ID and identical payload; persisted result readback; no new effect | NOT RUN |
| Fresh-ID duplicate approval/resume | Another new UUID in the same owner session; actual response and checkpoint/audit readback; effect count stays one | NOT RUN |
| Teardown | Separately authorized App/store/sink cleanup with ownership checks and independent outcome readback; retained shared resources identified | NOT RUN |

Store original invocation, wrong-principal resume, authorized resume, and fresh
duplicate invocation IDs separately. Worker replacement does not itself create a
new invocation: record replacement worker/attempt IDs, and a replacement
invocation ID only if one is actually created (with its relationship explained).
Never fabricate a recovery invocation for the paused request-user case. Capture
SDK-namespaced runtime IDs and checkpoint thread IDs from authorized readback
where available rather than assuming the public caller IDs are store keys.

For each boundary, collect authenticated HTTP invocation/result and event
readback **plus** independent persisted approval/checkpoint, effect-sink and audit
readback. Record actual status codes, timestamps, before/after counts, and
principal/correlation attribution. An immutable original interrupted output alone
cannot show that the current approval is still pending: inspect the current
checkpoint too. Readiness, persisted events, historical hard-worker-death
observations, and the local tests above do not replace these observations.
If audit persistence/readback or replacement attempt identity is unavailable,
record that gap rather than asserting it survived.

Link the sanitized committed live record from both this page and
[the consumer acceptance ledger](durable-host-consumers.md#evidence-and-retirement-gate).
Until every required live observation exists, retain BLOCKED status and do not
approve the acceptance review gate or claim consumer equivalence. A PR containing
this handoff is only a partial deliverable, not completion of apx-ab7.
