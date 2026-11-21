# apx-pzd: trace tags across a deployed worker replacement

**Observed 2026-11-21, UTC.** Operator authorization 2026-11-20.
[Sanitized machine-readable evidence](evidence.json).
[Fault-injection source](../../../scripts/probes/apx_pzd/server.py).
Local harness tests: `python/tests/test_durable_trace_probe.py`.

## Result

One invocation produced two MLflow trace records. Both carry
`apx.invocation.id` equal to that invocation. The recovered record has
`apx.recovery=true`. The client `traceparent` is a different trace id.
`MlflowClient.get_trace` could not load span payloads.

Worker-loss invocation `e45fcae3-ebb0-4df8-b31b-4b9da8576b3c`, session
`pzd-kill-4a5679d3-cffe-4299-aa9d-35f8ba2495de`:

- `POST /api/invocations` **202 / queued** at `2026-11-21T00:37:49.388288+00:00`. The request included `traceparent: 00-8f50350ad56a5b042a0ce02fdad9ede8-5aa77c0f102dd0f6-01`.
- `GET .../events` **200**. Cursor **44** was `pzd.attempt` attempt 1, `recovery=false`. Id 43 was `run.started`.
- `GET` stayed **200 / active** through `2026-11-21T00:38:28.942402+00:00` (17 polls). The probe flushed the open span before `os._exit(86)`, and that flush outlasted the watch window.
- `GET` at `2026-11-21T00:40:01.123918+00:00` was **502** (HTML, 11545 bytes, not JSON).
- App status **CRASHED**, message `App crashed unexpectedly at 2026-11-21T00:38:56Z`, `running_instances=0`. Compute stayed **ACTIVE**. Deployment id stayed `01f1e5431f3e1ef8b065d7d6324ca67c`.
- Attempt 1 file at `00:37:49.772746+00:00`, `recovery=false`, active trace `tr-6ceac8086d4f4ba5346c0ea535d07247`. Export file: `ended=true`, `flushed=true`, same trace id. Boundary checkpoint `1f1e543c-77d6-6e9f-8001-7b1820ec35a7`: `human, ai`, no tool result.
- Explicit `agentbricks deploy` of the same source started `2026-11-21T00:40:06Z` and finished `2026-11-21T00:41:59Z`. SNAPSHOT `01f1e5441f8e1de992b159c0e347aebd` switched traffic at `2026-11-21T00:40:31Z`. `databricks apps stop` / `start` was not used.
- Attempt 2 at `2026-11-21T00:42:03.766604+00:00`, `recovery=true`, active trace `tr-ef5152ac9ae8a1a69b85d7d064214df9`, same invocation, session, and pre-tool checkpoint id. The attempt-1 file was still present. There is no attempt 3.
- `GET` **200 / completed** at `2026-11-21T00:42:12.933703+00:00`. Final checkpoint `1f1e5445-f38f-617c-8003-22b25386d620`, step 3: `human, ai, tool, ai`. Metadata invocation id matches.

`MlflowClient.search_traces` on experiment `347424927259109` with
`tags.\`apx.invocation.id\` = 'e45fcae3-ebb0-4df8-b31b-4b9da8576b3c'` returned
**2** traces:

| Trace | `apx.recovery` | `request_time` | state | name |
|---|---|---|---|---|
| `tr-6ceac8086d4f4ba5346c0ea535d07247` | `false` | `2026-11-21T00:37:49.589000+00:00` | OK | `graph.invoke` |
| `tr-ef5152ac9ae8a1a69b85d7d064214df9` | `true` | `2026-11-21T00:42:03.515000+00:00` | OK | `graph.invoke` |

`execution_duration` on those records was 694 and 926. Tag names on both were
`apx.invocation.id`, `apx.recovery`, `mlflow.artifactLocation`,
`mlflow.traceName`, and `mlflow.user`. `apx.traceparent` was absent. Neither
trace id contains `8f50350ad56a5b042a0ce02fdad9ede8`.

`MlflowClient.get_trace` (local mlflow 3.14.0) raised
`MlflowException: Trace with ID ... cannot be loaded because it is missing span data`
for both of those ids and for the preflight ping trace
`tr-9ec7d99c1172fe58fd347d9a278b64a7`. `agentbricks tracing get` of the
recovered id exited 1. The warning repeated the missing-span-data text, and
the JSON error was `No trace found with id 'tr-ef5152ac9ae8a1a69b85d7d064214df9'`.
The join above is the `search_traces` tag record, not a downloaded span tree.

From the crash timestamp to attempt 2 was 187.8 seconds. That includes the
watch window ending before the exit, the wait to start the redeploy, and the
new process boot. `running_instances` stayed 0 until the new SNAPSHOT.

## Symbols

`stamp_invocation_correlation` in `apx_agent._audit` stamps
`apx.invocation.id` and `apx.recovery`. The durable turn calls it inside
`safe_span("graph.invoke")` in `apx_agent._graph_turn`. The exported
`mlflow.traceName` is `graph.invoke`.

`continue_trace_from_headers` in `apx_agent._mlflow_tracing` is what the
compatibility host uses from `_invocations._inbound_request_span`.
`DurableAgentServer._invoke_request` in databricks-agentkit 0.10.0
`runtime/app.py` does not call it. `databricks_agentkit.runtime.tracing.start_trace`
opens `mlflow.start_span` and does not read `traceparent`.
`DurableAgentServer._execute` does not call `start_trace`. This run does not
continue an inbound trace header.

On the non-recovery path the probe calls `span.end()` and
`mlflow.flush_trace_async_logging` before `os._exit(86)`. That flush is
probe-side so the first trace info can leave the process. It is not a product
close of `safe_span`. The recovery path returns and does not exit. The missing
span blob also happened on the completed ping and on the recovered trace, so
it is not only that early end.

## App and versions

App `agent-bricks-apx-dur-gen`, workspace `7474660401027952`, profile
`fevm-stable`, `compute_size=MEDIUM`, one instance. In-app versions: Python
3.15.1, apx-agent `0.7.8.dev8+g0c15c9f84`, databricks-agentbricks 0.10.0,
databricks-sdk 0.169.0, langchain 1.5.2, langchain-core 1.8.4, langgraph
1.2.24, langgraph-checkpoint 4.3.2, mlflow 3.18.0. CLI 1.26.0. The trace
reader was mlflow 3.14.0. Package index `https://pypi.org/simple/`.

Ingress was Databricks CLI U2M OAuth (`auth_type=databricks-cli`) through
`apps_inbound_oauth_headers`. `[auth.user] required = false`, so the tool ran
as app service principal `06c3f57c-f656-4872-b791-33106ce1ad0b`. Request-user
recovery was not enabled.

Session Store `apx-pzd-1121-sessions`, UID
`0d45531c-f23d-4849-a0e4-6498743b8e86`, created `2026-11-21T00:32:55.082Z`.
Runtime database `agent-bricks-apx-dur-gen`, schema
`databricks_agentkit_runtime`. Experiment `/Shared/apx-pzd-1121-traces`, id
`347424927259109`. Diagnostic directory `/Shared/apx-pzd-1121-diag`, object id
`347424927259108`.

A quiet ping before the fault returned **202** then **200 / completed** `pong`
(`62156c60-04f5-4605-8539-fa407a727302`) on deployment
`01f1e5431f3e1ef8b065d7d6324ca67c`. Its trace
`tr-9ec7d99c1172fe58fd347d9a278b64a7` had the same invocation id and
`apx.recovery=false`. After recovery, ping
`a11fe9f8-4b36-4355-b78f-a10f8695efba` returned **202** at
`2026-11-21T00:45:06.358432+00:00` and **200 / completed** `pong` at
`2026-11-21T00:45:09.600217+00:00`. The app was **RUNNING**,
`running_instances=1`, deployment `01f1e5441f8e1de992b159c0e347aebd`.

## Left running

The app, session store, experiment, managed runtime database, and diagnostic
directory were not deleted. Parked app `agent-bricks-stu-dur-1002` was not
touched. Inbound trace continuation on the native host remains unsupported, so
this bead stays open.
