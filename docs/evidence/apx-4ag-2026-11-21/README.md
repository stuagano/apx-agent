# apx-4ag: disconnect, replay, and worker replacement

**Observed 2026-11-21, UTC.** Operator authorization 2026-11-20.
[Sanitized machine-readable evidence](evidence.json).
[Fault-injection source](../../../scripts/probes/apx_4ag/server.py).
Local harness tests: `python/tests/test_durable_disconnect_probe.py`.

## Result

Closing the event stream left the invocation running. Replacing the worker
replayed the same invocation from the saved cursor. Native cancellation was
not available.

Hold invocation `c5a4b04e-e38b-4797-bbee-2a49591ba6f6`, session
`4ag-hold-5cfb0678-2a57-473d-a875-972c622b086d`:

- `POST /api/invocations` **202 / queued** at `2026-11-21T00:13:28.968118+00:00`.
- `GET .../events` **200**. The client recorded cursor **22** (`4ag.attempt`, attempt 1, `recovery=false`) and closed the stream at `00:13:29.465062+00:00`.
- `GET` at `00:13:29.887605+00:00` was **200 / active**.
- `POST .../cancel` **404** `{"detail":"Not Found"}`. `DELETE` of the invocation **405** `{"detail":"Method Not Allowed"}`. The following `GET` was still **200 / active**.
- `GET` at `00:13:43.640597+00:00` was **200 / completed**. The hold diagnostic files were still present.
- `GET .../events?after=22` **200** returned ids **23–26** (`agent.message.delta` three times, then `run.completed`). Id 22 was not repeated. The unfiltered stream returned **21–26**.

Worker-loss invocation `eb618495-b483-4ea1-a86e-9eb734c6a708`, session
`4ag-kill-9613d8e9-eb57-484f-8c96-1fa761bc75bd`:

- `POST` **202 / queued** at `00:14:42.681720+00:00`.
- The client closed `GET .../events` at cursor **28** (`4ag.attempt`, attempt 1, `recovery=false`) at `00:14:43.293930+00:00`.
- `GET` stayed **200 / active** at `00:14:43.705691+00:00` and `00:14:46.130296+00:00`, then **502** at `00:14:48.639913+00:00`.
- App status **CRASHED**, message `App crashed unexpectedly at 2026-11-21T00:14:47Z`, `running_instances=0`. Compute stayed **ACTIVE**. Deployment id was unchanged.
- Attempt 1 boundary checkpoint `1f1e5408-cf8e-68a5-8001-c223eda71620`: `human, ai`, no tool result. The attempt-1 file was still there after the replacement worker finished.
- Explicit `agentbricks deploy` of the same source, started `00:15:14Z`. SNAPSHOT `01f1e540a39a1e87b454a2b09081c5e9` switched traffic at `00:15:35Z`. `databricks apps stop` / `start` was not used.
- Attempt 2 at `00:17:10.126431+00:00`, `recovery=true`, same invocation, session, and pre-tool checkpoint id.
- `GET` **200 / completed** at `00:17:24.564751+00:00`. Final checkpoint `1f1e540e-c179-660c-8003-1c75693fa153`: `human, ai, tool, ai`.
- `GET .../events?after=28` **200** returned ids **29–34**, including `4ag.attempt` id 31 (`recovery=true`) and `run.completed` id 34. The unfiltered stream returned **27–34**, so attempt 1 cursor 28 is still in the log.

From the crash timestamp to attempt 2 was 143.1 seconds. That includes the wait to start the redeploy and the new process boot. It is an observed interval, not a latency guarantee. `running_instances` stayed 0 until the new SNAPSHOT. There was no second replica.

`DurableAgentServer` in databricks-agentkit 0.10.0 registers only `POST /api/invocations`, `GET /api/invocations/{invocation_id}`, and `GET /api/invocations/{invocation_id}/events` (`runtime/app.py` lines 133–139). `_events` takes `after: int = 0` (line 314). The store keeps events with `sequence_number > after_sequence` (`runtime/store.py` line 302). `CancellationRegistry` in `apx_agent._cancellation` is still in the tree. This probe does not register it, and these handlers do not grow a cancel route.

Request-user crash recovery was not enabled. Ingress was Databricks CLI U2M OAuth (`auth_type=databricks-cli`) through `apps_inbound_oauth_headers`. The manifest sets `[auth.user] required = false`, so the tool ran as app service principal `06c3f57c-f656-4872-b791-33106ce1ad0b`.

## App and versions

App `agent-bricks-apx-dur-gen`, workspace `7474660401027952`, profile `fevm-stable`, `compute_size=MEDIUM`, one instance. In-app versions on both kill attempts: Python 3.15.1, apx-agent `0.7.8.dev8+g0c15c9f84`, databricks-agentbricks 0.10.0, databricks-sdk 0.169.0, langchain 1.5.2, langchain-core 1.8.4, langgraph 1.2.24, langgraph-checkpoint 4.3.2. CLI 1.26.0. Package index `https://pypi.org/simple/`. Deployed without tracing.

Session Store `apx-4ag-1120-sessions`, UID `b9e1f296-dc58-4e0a-911f-59cd8c5e9195`, created `2026-11-21T00:08:58.120Z`. Runtime database `agent-bricks-apx-dur-gen`, schema `databricks_agentkit_runtime`. Diagnostic directory `/Shared/apx-4ag-1120-diag`. The attached `postgres-runtime-store` resource was not removed.

A quiet ping before the hold returned **202** then **200 / completed** `pong` (`afec9ff0-fed1-44b8-8805-731197685c2d`). After recovery, ping `46c37341-7248-4a50-9ae7-d1dea90c22be` did the same: **202** at `00:18:55.790867+00:00`, **200 / completed** `pong` at `00:18:59.895344+00:00`. The app was **RUNNING**, `running_instances=1`, deployment `01f1e540a39a1e87b454a2b09081c5e9`.

## Left running

The app, session store, managed runtime database, legacy postgres resource, and diagnostic directory were not deleted. Parked app `agent-bricks-stu-dur-1002` was not touched.
