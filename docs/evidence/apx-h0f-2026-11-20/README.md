# apx-h0f: same session and invocation after deployed worker loss

**Observed 2026-11-20, UTC.** [Sanitized machine-readable evidence](evidence.json).
[Fault-injection source](../../../scripts/probes/apx_h0f/server.py).
Local harness tests: `python/tests/test_durable_session_probe.py`.

## Result

One invocation stayed the same invocation across a hard process exit.
`POST /api/invocations` returned **202 / queued** for
`470246f7-8dda-4f3d-8cdb-7394fecc64b5`, session
`h0f-session-2a5aa8f5-a06b-4ca4-a0f0-19da59aec46f`. Attempt 1
(`recovery=false`) checkpointed the user message and the tool call, then
called `os._exit(86)` before the tool returned. The operator's independent
Session Store read showed that same checkpoint and no tool result. `GET` of
the invocation then returned **502**. An explicit redeploy started a new
worker. Attempt 2 (`recovery=true`) finished the **same** invocation and
session. `GET` returned **200 / completed**. The original human message id and
tool-call message id were still in the managed checkpoint. A repeat `POST` of
the same body returned **202 / completed** and did not write attempt 3.

This is the session/invocation item for `apx-h0f` only. It does not prove
request-user recovery, cancellation, trace joins, downstream OBO, exactly-once
effects, or automatic restoration of the dead worker. No production runtime
machinery was removed.

## App, identity, and stores

The operator authorized the existing disposable App
`agent-bricks-apx-dur-gen` (workspace `7474660401027952`, profile
`fevm-stable`, space-less **MEDIUM**, `compute_min_instances=1`). Ingress used
a Databricks CLI U2M OAuth access token through `apps_inbound_oauth_headers`.
Not a PAT. The agent manifest sets `[auth.user] required = false`. The tool
ran as the App service principal `06c3f57c-f656-4872-b791-33106ce1ad0b`.
Request-user crash recovery was not enabled and was not imitated by switching
credentials.

- Session Store `apx-h0f-1120-sessions`, UID `d6b2eb8d-490e-4a95-bc19-c6403d7d7d3e`, created for this run and left in place.
- Runtime binding injected by Agent Bricks: database `agent-bricks-apx-dur-gen`, schema `databricks_agentkit_runtime`, branch `projects/databricks-internal-custom-agents/branches/production`, username the App principal.
- The App's older `postgres-runtime-store` resource (`projects/agent-bricks-apx-dur-gen-runtime-store`) is still attached. This run did not delete it. The deployed env does not point the SDK at that legacy endpoint.
- Diagnostic files under `/Shared/apx-h0f-1120-diag` record attempt boundaries. They are not a business effect.

The probe is `LlmAgent` plus `compile_durable_handlers(..., recovery=True)`, the SDK `DurableAgentServer`, and `DatabricksSessionStoreSaver`. The model is the deterministic test seam, not a paid endpoint. In-app versions, identical on both attempts: Python 3.15.1, apx-agent `0.7.8.dev8+g0c15c9f84`, databricks-agentbricks 0.10.0, databricks-sdk 0.169.0, langgraph 1.2.24. Databricks CLI 1.26.0. Wheel and manifest hashes are in `evidence.json`.

## Replacement, not stop/start and not a second replica

| When | What was observed |
|---|---|
| 23:48:15Z | SNAPSHOT `01f1e53ca10d1b999edcd8bfe28aa517` serving. A non-crash ping completed `pong`. |
| 23:50:50Z | Crash invocation accepted, HTTP 202. |
| 23:50:51Z | Attempt 1 boundary. Checkpoint `1f1e53d3-7b09-65d1-8001-f8d112ebfbd5`: `human, ai`, no tool result, no pending tool write. Invocation id is in `databricks_agentkit.invocation_id` and `apx_turn`. |
| 23:50:53Z | App status later read as **CRASHED**, `running_instances=0`. Compute stayed **ACTIVE** (`active_instances=1`). The active deployment id had not changed. |
| 23:50:54Z | `GET /api/invocations/{id}` **502** (HTML body omitted). |
| 23:51:38Z–23:53:28Z | Explicit `agentbricks deploy` of the same source. New SNAPSHOT `01f1e53d58751383b86e77e91dca86e6` switched traffic at 23:52:00Z. `databricks apps stop` / `start` was not used. |
| 23:53:36Z | Attempt 2, `recovery=true`, same invocation, session, and pre-tool checkpoint id. |
| 23:53:39Z | `GET` **200 / completed**. Session checkpoint `1f1e53d9-a47e-6cc7-8003-db2850afc237`: `human, ai, tool, ai`. Thread id `d6c3a35836847ed463dc1a714cc42460ceea7f286f1d1bf018dba4a90cc3ffd2` matches the HTTP result and the pre-crash `actor_id`. |
| 23:53:42Z | Duplicate POST **202 / completed**. Diagnostic listing stayed four files. |
| 23:54:28Z | A new ping session completed `pong`. App **RUNNING**, `running_instances=1`. |

From `os._exit` to attempt 2 was about 165 seconds. That includes waiting to start the redeploy and booting the new process. It is an observed interval, not a latency guarantee. Nothing in this run restored the dead process by itself: `running_instances` was 0 while compute still reported a node. There was no second replica to fail over to. The historical two-replica note in `runtime-targets.md` is a different deployment and was not re-run.

Event replay (`GET .../events`, **200**, `text/event-stream`) from the replacement worker contains attempt 1 `recovery=false`, then attempt 2 `recovery=true`, then `run.completed`, all with the original invocation and session ids. Returned cursors were 5–12. This is persisted invocation history, not a second submission. Disconnect was not exercised, and it is not cancellation.

## Left running

The App, the new Session Store, the managed Runtime Store binding, the legacy postgres resource, and the diagnostic directory were **not** deleted. Parked App `agent-bricks-stu-dur-1002` was not touched. Stores named for the other managed-state probes were not deleted.
