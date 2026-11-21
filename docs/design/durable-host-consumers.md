# Durable host consumers (#864)

**Status:** inventory frozen; apx-pzd recorded a live trace-tag join on 2026-11-21; native handlers still do not continue an inbound traceparent · **Date:** 2026-11-20 ·
**Issue:** [#864](https://github.com/stuagano/apx-agent/issues/864) ·
**Parent:** [#862](https://github.com/stuagano/apx-agent/issues/862)

Native Apps already compile to `DurableAgentServer` (`python -m apx_agent._serve`).
This page records which ChatAgent / Responses / AppKit surfaces still have
callers, so they are not retired until each caller has a proven native
equivalent. Local recovery tests in `test_durable_recovery.py` are not live
worker-loss proof.

`python/tests/test_durable_host_consumers.py` freezes the production import
set. If you add a ChatAgent caller, update both that test and this page. If
you want to remove one, migrate the caller first.

## Already retired

| Surface | Evidence |
|---|---|
| Generated AppKit host, TypeScript agent runtime, Python tool bridge | [apx-internal-appkit-host.md](apx-internal-appkit-host.md); tests assert `apx_appkit_host` is not generated |

## Native Apps host (keep)

`apx_agent._serve.create_app` → `compile_agent(..., target="durable_agent_server")`.
No `chat_agent_for`. Transport is `POST /api/invocations` plus read-back and
event replay. There is no cancel / disconnect / reconnect route
([runtime-targets](../running/runtime-targets.md)).

Request-user recovery stays rejected. Service-identity recovery is opt-in
(`recovery = true`). Local APX recovery tests and later deployed lifecycle
observations are distinct evidence; neither closes all six acceptance items
below.

## ChatAgent / Model Serving (keep until those products move)

| Production module | Why it still uses ChatAgent |
|---|---|
| `_chat_agent.py` | `chat_agent_for` / `compile_to_chat_agent` / `log_agent` |
| `cli.py` | Model Serving deploy (`log_agent`); `eval test` smoke (`compile_to_chat_agent`) |
| `_run_once.py` | One-shot local/Jobs invoke |
| `_eval.py` | Scored eval runs |
| `_dev.py` | Dev-UI compile path |
| `_invocations.py` | Legacy `POST /invocations` on the compatibility FastAPI host |
| `_a2a.py` | Compatibility-host A2A `message/send` shares that ChatAgent |
| `_wiring.py` `create_app` | Mounts `/invocations` and A2A for `responses_agent` Apps and local `create_app` |

Native DurableAgentServer does **not** call these. Deleting `_chat_agent.py`
would break Model Serving deploy, eval, `eval test`, Jobs `run_once`, the
Responses compatibility Apps host, and A2A on that host.

## Responses compiler (keep compatibility)

`compile_to_responses_agent` remains for MLflow packaging (`_mlflow_model.py`)
and `/readyz` on the compatibility host. Explicit `target = "responses_agent"`
scaffolds still generate `agent_server/start_server.py` → `create_app` in
`_wiring.py`.

## Live #864 acceptance ledger (apx-fgj)

On **2026-11-20**, apx-fgj used its acceptance option to split each unproven
live item into its own bead with a named blocker. **No new deployed run was
performed**: this assignment forbids deploys and operations on running workers.
These are open follow-ups in Beadhive hive `apx`, each linked to apx-fgj by
`discovered-from`; splitting the work does not establish live equivalence or
close the platform's remaining acceptance. The old probe's Apps OAuth failure
below is historical, not a claim that OAuth is unavailable today.

| Live item | Follow-up bead | Blocker / boundary | Required deployed proof |
|---|---|---|---|
| Session/invocation round-trip after worker loss | `apx-h0f` | No operator-authorized disposable App, fault-injection run, or Apps OAuth context supplied for this assignment | Same invocation and session read back over HTTP and managed storage across real worker loss; original/recovery attempts and replacement mechanism recorded |
| Replay-safe external effects | `apx-aub` | No authorized disposable effect sink or crash-window probe; uncommitted work is at-least-once | Kill after an external write but before its tool-result checkpoint; independent sink and checkpoint readback prove stable-key deduplication or reconciliation, with retry counts and residual limits |
| Authorized approval continuation after replacement | `apx-ab7` | No authorized App and distinct OAuth principals; request-user automatic recovery remains unsupported | Pending approval survives without effects; wrong principal is denied, authorized fresh continuation succeeds once, duplicate resume cannot repeat the effect; retain original and new resume invocation IDs in the same session |
| Trace correlation after real replacement | `apx-pzd` | [2026-11-21 evidence](../evidence/apx-pzd-2026-11-21/README.md): two roots join on `apx.invocation.id` and the recovered tag is `apx.recovery=true`. `DurableAgentServer._invoke_request` does not call `continue_trace_from_headers`. `MlflowClient.get_trace` could not load span data | Read back initial/recovered trace IDs and actual invocation record; separate roots join on `apx.invocation.id`, recovered trace has `apx.recovery=true` |
| Disconnect/reconnect/cancellation contract | `apx-4ag` | No authorized OAuth SSE/replacement probe; native cancellation API is absent | Persisted cursor replay after disconnect and replacement, invocation/effect readback, attempt boundaries; explicitly document that disconnect is not cancellation or rollback and cancellation remains unsupported unless independently proven |
| Native downstream delegation with caller identity | `apx-gar` | Request-user recovery cannot reconstruct transient credentials; no authorized two-App/OAuth/UC audit probe supplied | Parent/child invocation and result/events readback plus per-hop caller/scopes audit; unauthorized or missing credentials fail closed, no service-identity fallback; coordinate [#816](https://github.com/stuagano/apx-agent/issues/816) / [#755](https://github.com/stuagano/apx-agent/issues/755) |

### Existing evidence and its limits

The [observed deployed lifecycle](../running/runtime-targets.md#observed-deployed-lifecycle)
records later live hard-worker-death observations: a single worker needed a
redeploy before a replacement scan loop replayed the original input; a separate
two-replica hand-written LangGraph deployment resumed an unfinished node via the
surviving replica. Neither observation establishes automatic restoration of dead
replica capacity. These observations supersede a blanket "local tests only"
claim about durable execution, but do not supply the full per-item acceptance
record above (notably APX HTTP/session round-trip, external crash-window effects,
principal-scoped approvals, trace joins, stream lifecycle, and downstream OBO).
Do not infer those proofs or invent missing run dates, invocation IDs or traces.

On **2026-11-21**, disposable app `agent-bricks-apx-dur-gen` (one instance)
lost its worker inside a tool and came back only after an explicit SNAPSHOT
redeploy. Invocation `e45fcae3-ebb0-4df8-b31b-4b9da8576b3c` read back **200 /
completed**. `search_traces` filtered on `tags.\`apx.invocation.id\`` returned
`tr-6ceac8086d4f4ba5346c0ea535d07247` (`apx.recovery=false`) and
`tr-ef5152ac9ae8a1a69b85d7d064214df9` (`apx.recovery=true`). The client
`traceparent` trace id `8f50350ad56a5b042a0ce02fdad9ede8` is neither of those.
`stamp_invocation_correlation` (`apx_agent._audit`) runs inside
`safe_span("graph.invoke")` in `apx_agent._graph_turn`.
`continue_trace_from_headers` (`apx_agent._mlflow_tracing`) is called from the
compatibility host `_invocations._inbound_request_span`.
`DurableAgentServer._invoke_request` does not call it. `MlflowClient.get_trace`
and `agentbricks tracing get` reported missing span data, so the join is the
trace-tag record. The full sanitized run is
[apx-pzd-2026-11-21](../evidence/apx-pzd-2026-11-21/README.md). That run does
not prove inbound header continuation, request-user recovery, a native cancel
API, downstream OBO, or exactly-once effects.

`test_durable_recovery.py` reconstructs handlers/savers against a local REST fake.
It covers committed-tool recovery, approval pause/resume, budgets and invocation
trace tags, not a deployed worker replacement or authorization across principals.
A `/readyz` `durable: true` result or replay of persisted events is not crash
recovery proof. Request-user crash recovery remains rejected; do not silently
switch to app/service credentials to make a probe succeed.

### Evidence and retirement gate

Each follow-up must link a dated, committed, sanitized run record here: App and
workspace, code/SDK versions, identity mode, store bindings, fault boundary and
replacement trigger, session/invocation/attempt IDs, and independent HTTP plus
store/effect/audit readback appropriate to the item. Record observed failures and
unsupported capabilities as well as successes. Never commit tokens or secrets.
Obtain operator authorization before provisioning, fault injection or teardown;
these beads are not permission to operate on production.

Managed-store startup/grant evidence is tracked separately by `apx-l79`
(#855 residual). Its [2026-11-20 probe runbook](managed-state-contract-map.md#855-residual-probe-preparation-apx-l79-2026-11-20)
adds opt-in grant readback, typed denied/unsupported-service failures and an
App-process lifespan check of the declared memory and session bindings. **No
new live App or grant run was performed**: deployment is prohibited for this
assignment and no authorized App/second-principal/failure-workspace context was
provided. Those acceptance items remain unverified. Local SDK-transport tests,
a successful `/readyz`, or constructing a saver do not close this gate; retain
the actual deployment/run and independent binding evidence before claiming
live acceptance. The probe does not deploy or enable recovery, and must not be
used to restart a production listener.

**No custom runtime machinery is removed by apx-fgj.** ChatAgent consumers and
compatibility routes above, policy/approval/audit wiring and the legacy
cancellation wrapper stay in place. Retirement requires a recorded equivalence
proof for each migrated consumer, including identity and governance behavior;
these open beads and historical lifecycle observations are not that proof.
Do not retry parked app `agent-bricks-stu-dur-1002` (#863), or reuse the torn-down
`agent-bricks-864probe` as though it still exists.

## Disposable Apps probe (`agent-bricks-864probe`, 2026-10-21)

Fresh App Space app on profile `DEFAULT`
(`https://fevm-serverless-stable-7def3z.cloud.databricks.com`). Source:
`/home/sandbox-agent/apx-864-probe` (not in git). Name prefix
`agent-bricks-*`. `recovery = true`. Managed session store
`apx-864probe-sessions`. Public URL
`https://agent-bricks-864probe-7474660401027952.aws.databricksapps.com`.

| Event | Evidence |
|---|---|
| First SNAPSHOT | `01f1cd4f535c13cb935b391653b1aaa5` SUCCEEDED, app RUNNING, compute ACTIVE |
| Deploy CLI `/readyz` | `ok: false` — `databricks auth token` fails: `OAuth Token not supported for current auth type pat` |
| Workspace Apps API | `GET /api/2.0/apps/agent-bricks-864probe` works with PAT |
| App URL `/readyz` | PAT GET is SSO (`302` to workspace `/oidc/oauth2/v2.0/authorize`, or Sign In HTML). Not application JSON |
| `databricks apps logs` | Same PAT OAuth-token error |
| `apps stop` then `start` | App went `UNAVAILABLE` / compute `STOPPED`, `active_deployment` empty. Start rolled a **new** SNAPSHOT `01f1cd4fe31614e4aff284bfc86b65ac` (`App started successfully`, RUNNING/ACTIVE). This is not same-process resume |

HTTP invoke (`POST /api/invocations` or `apx-agent agents query --profile`)
did not complete. PAT cannot mint `databricks auth token`. PAT cannot invoke
an Apps URL (SSO); inbound API calls need U2M/M2M OAuth
(`apps_inbound_oauth_headers`). Cursor local-agent
OAuth sent the callback to `http://localhost:8020/__arca_oauth_pick` (Arca
instance picker). CLI 1.20.0 also ignores `DATABRICKS_OAUTH_CALLBACK_PORT`,
so moving the listener to 8021 did not help.

**Teardown (2026-10-21):** `apx-agent destroy --profile DEFAULT --yes` deleted
the app (compute `DELETING` → `NotFound`). `AgentKitClient.session_stores`
deleted `apx-864probe-sessions`. Workspace source
`/Workspace/Users/stuart.gano@databricks.com/agentbricks_deployments/agent-bricks-864probe`
was removed. Did not touch `van-bsmmyz-sessions` or parked
`agent-bricks-stu-dur-1002`.

## Parent #862 remaining (2026-10-21)

Local tests and this inventory are not live workspace acceptance. GitHub issue
bodies still stop at `6cdcff86` (2026-10-06).

| Issue | Code / evidence | Still open for |
|---|---|---|
| #855 / #856 | Live disposable store probe on DEFAULT; opt-in `APX_MANAGED_STATE_LIVE=1` | Retention, grants, App-process startup; GitHub checklists |
| #857 | Lakebase conversation history on in-repo examples | `platform-health` / `product-finder` are not in this repo |
| #858 | `apx-agent memory extract` / `_memory_extract.py` | GitHub text still says extraction is unshipped |
| #864 | Consumer freeze, historical OAuth probe, and later [deployed lifecycle observations](../running/runtime-targets.md#observed-deployed-lifecycle) | Six open live acceptance beads in the [apx-fgj ledger](#live-864-acceptance-ledger-apx-fgj); no consumer equivalence proof |
| #575 | Wait | UC Agent Services GA |
| #909 | Agent Bricks forces `agent-bricks-*` | MCP `mcp-` discovery; workaround is App Space `apps_api` |
| #911 | Doctor names services that need a UI guardrail check | Attachment is UI-only |
| #946 / #947 | #947: templating + partition shipped; ConversationStore scoped methods rejected | #946 TS vs Python client; #947 durable `user:`/`app:` backend |
| #954 | Checks, deploy, doctor, native launcher, and `config_from_agent` read Python constructor settings when there is no `AgentConfig` | `[tool.apx.agent]` remains valid until sub-project 3 removes it |
| #836 | `session_budget={"usd": D}` on served turns, priced from usage metadata | Live billing vs published FMAPI list prices |
