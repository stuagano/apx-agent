# apx-aub: replay-safe external effect on agent-bricks-apx-dur-gen

**Observed 2026-11-21, UTC.** Operator authorization 2026-11-20.
[Sanitized machine-readable evidence](evidence.json).
[Fault-injection source](../../../scripts/probes/apx_aub/server.py).
The [2026-11-20 record](../apx-aub-2026-11-20/README.md) is a different app,
`agent-bricks-aub-1120-2018`, which that run tore down. This page is the run
on the still-live disposable app.

## Result

Invocation `0d89a816-f94a-496c-a4e2-a0dbc2fe65a0`, session
`aub-session-652f166c-742a-4868-8870-09187eb2023d`, business key
`aub-business-20261121-dur-gen`.

- `POST /api/invocations` **202 / queued** at `2026-11-21T00:56:19.641632+00:00` on SNAPSHOT `01f1e545e5aa1aeaaf631e9d52db70cf`.
- Attempt 1 at `2026-11-21T00:56:28.710707+00:00`, `recovery=false`, `created=true`. Independent sink readback was `{"effect_key":"aub-business-20261121-dur-gen","value":"harmless-test-mutation"}`. Checkpoint `1f1e5466-26d5-6f32-8001-4cfde084eb40`: `human, ai`, no tool result, no pending writes. Then `os._exit(86)`.
- `GET` was **200 / active** at `00:56:20.056376+00:00` and `00:56:27.363319+00:00`, then **502** at `00:56:29.867947+00:00`.
- App status **CRASHED**, message `App crashed unexpectedly at 2026-11-21T00:56:30Z`, `running_instances=0`. Compute stayed **ACTIVE**. Deployment id was unchanged.
- Explicit `agentbricks deploy` of the same source started `2026-11-21T00:56:47Z` and finished `2026-11-21T00:58:35Z`. SNAPSHOT `01f1e546718010678017040e46a17107` switched traffic at `2026-11-21T00:57:08Z`. `databricks apps stop` / `start` was not used.
- Attempt 2 at `2026-11-21T00:58:41.593553+00:00`, `recovery=true`, `created=false`, same invocation, session, effect key, and pre-tool checkpoint id. The sink body was unchanged. Attempt 1 was still present. There is no attempt 3.
- `GET` **200 / completed** at `2026-11-21T00:58:52.109953+00:00`. Tool content: `{"created": false, "effect_key": "aub-business-20261121-dur-gen"}`. Final checkpoint `1f1e546b-2291-6355-8003-9c729ea7a0f4`, step 3: `human, ai, tool, ai`.

Raw tool attempts: **2**. Effective business mutations: **1**. The second
`workspace.upload(..., overwrite=False)` hit the existing object and the
readback matched, so the tool returned `created=false` instead of writing a
second body.

From the crash timestamp to attempt 2 was 131.6 seconds. That includes the
wait to start the redeploy and the new process boot. `running_instances`
stayed 0 until the new SNAPSHOT. There was no second replica.

This is not exactly-once execution. Dedup is the stable key plus a create-only
upload. A different existing body would have raised instead of being
overwritten; that path was not the live case. No second POST was sent. A new
non-recovery user message on this snapshot still calls `os._exit(86)`.

## App and versions

App `agent-bricks-apx-dur-gen`, workspace `7474660401027952`, profile
`fevm-stable`, `compute_size=MEDIUM`, one instance. In-app versions: Python
3.15.1, apx-agent `0.7.8.dev9+ge67f146a3`, databricks-agentbricks 0.10.0,
databricks-sdk 0.169.0, langchain 1.5.2, langchain-core 1.8.4, langgraph
1.2.24, langgraph-checkpoint 4.3.2. CLI 1.26.0. Package index
`https://pypi.org/simple/`. Deployed without tracing.

Ingress was Databricks CLI U2M OAuth (`auth_type=databricks-cli`) through
`apps_inbound_oauth_headers`. `[auth.user] required = false`, so the tool ran
as app service principal `06c3f57c-f656-4872-b791-33106ce1ad0b`. Request-user
recovery was not enabled.

Session Store `apx-aub-1121-sessions`, UID
`02de0240-7def-4325-8db4-361083014597`, created `2026-11-21T00:52:53.230Z`.
Runtime database `agent-bricks-apx-dur-gen`, schema
`databricks_agentkit_runtime`. Sink directory `/Shared/apx-aub-1121-sink`,
object id `2683107657279850`.

After recovery the app was **RUNNING**, `running_instances=1`, deployment
`01f1e546718010678017040e46a17107`, observed
`2026-11-21T00:58:54.828307+00:00`.

## Left running

The app, session store, sink, and managed runtime database were not deleted.
Parked app `agent-bricks-stu-dur-1002` was not touched.
