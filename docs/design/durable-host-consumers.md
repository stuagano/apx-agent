# Durable host consumers (#864)

**Status:** inventory frozen; session/invocation proof recorded, five live follow-ups remain · **Date:** 2026-11-20 ·
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
below. The separately authorized [apx-h0f session/invocation run](../evidence/apx-h0f-2026-11-20/README.md)
closes only that item, not the other five or consumer retirement.

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
live item into its own bead with a named blocker. **No deployed run was
performed by that splitting assignment**, which prohibited deploys and worker
operations. The follow-ups are in Beadhive hive `apx`, each linked to apx-fgj by
`discovered-from`; splitting alone did not establish live equivalence.
Subsequently, the operator explicitly authorized the `apx-h0f` run recorded
below on the existing disposable App `agent-bricks-apx-dur-gen`. That evidence
does not close the platform's remaining acceptance. The old probe's Apps OAuth
failure below is historical, not a claim that OAuth is unavailable today.

| Live item | Follow-up bead | Blocker / boundary | Required deployed proof |
|---|---|---|---|
| Session/invocation round-trip after worker loss | `apx-h0f` | **Proof recorded, 2026-11-20:** service identity on one MEDIUM instance; `os._exit` before the tool returned; explicit replacement SNAPSHOT, not a surviving replica or `apps start` | [Run report and sanitized readbacks](../evidence/apx-h0f-2026-11-20/README.md): same invocation and session, attempts 1/2, HTTP 202 then 502 then 200, managed checkpoint message ids continued. Not request-user recovery or automatic capacity restoration |
| Replay-safe external effects | `apx-aub` | No authorized disposable effect sink or crash-window probe; uncommitted work is at-least-once | Kill after an external write but before its tool-result checkpoint; independent sink and checkpoint readback prove stable-key deduplication or reconciliation, with retry counts and residual limits |
| Authorized approval continuation after replacement | `apx-ab7` | No authorized App and distinct OAuth principals; request-user automatic recovery remains unsupported | Pending approval survives without effects; wrong principal is denied, authorized fresh continuation succeeds once, duplicate resume cannot repeat the effect; retain original and new resume invocation IDs in the same session |
| Trace correlation after real replacement | `apx-pzd` | No authorized fault-injection App with readable MLflow traces; the invocation-tag join has local tests only | Read back initial/recovered trace IDs and actual invocation record; separate roots join on `apx.invocation.id`, recovered trace has `apx.recovery=true` |
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
record above (notably external crash-window effects, principal-scoped approvals,
trace joins, stream lifecycle, and downstream OBO). The APX HTTP/session
round-trip now has a separate [apx-h0f acceptance record](../evidence/apx-h0f-2026-11-20/README.md);
its single-worker service-identity harness is not full consumer equivalence and
does not replace the two-replica observation.
Do not infer the other proofs or invent missing run dates, invocation IDs or traces.

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

**No custom runtime machinery is removed by apx-fgj.** ChatAgent consumers and
compatibility routes above, policy/approval/audit wiring and the legacy
cancellation wrapper stay in place. Retirement requires a recorded equivalence
proof for each migrated consumer, including identity and governance behavior;
these remaining open beads, historical lifecycle observations, and the scoped
session/invocation harness are not that proof.
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
| #864 | Consumer freeze, historical OAuth probe, later [deployed lifecycle observations](../running/runtime-targets.md#observed-deployed-lifecycle), and [apx-h0f session/invocation proof](../evidence/apx-h0f-2026-11-20/README.md) | Five remaining live acceptance items in the [apx-fgj ledger](#live-864-acceptance-ledger-apx-fgj); no consumer equivalence proof |
| #575 | Wait | UC Agent Services GA |
| #909 | Agent Bricks forces `agent-bricks-*` | MCP `mcp-` discovery; workaround is App Space `apps_api` |
| #911 | Doctor names services that need a UI guardrail check | Attachment is UI-only |
| #946 / #947 | #947: templating + partition shipped; ConversationStore scoped methods rejected | #946 TS vs Python client; #947 durable `user:`/`app:` backend |
| #954 | Checks, deploy, doctor, native launcher, and `config_from_agent` read Python constructor settings when there is no `AgentConfig` | `[tool.apx.agent]` remains valid until sub-project 3 removes it |
| #836 | `session_budget={"usd": D}` on served turns, priced from usage metadata | Live billing vs published FMAPI list prices |
