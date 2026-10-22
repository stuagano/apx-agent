# Durable host consumers (#864)

**Status:** inventory frozen; live HTTP continuity blocked on Apps OAuth · **Date:** 2026-10-21 ·
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
(`recovery = true`) and has **local** tests only.

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

## Live #864 gaps (not closed by this inventory)

- Worker-loss / process replacement on a real App Space app **with a
  session/invocation round-trip**
- Streaming disconnect / reconnect / cancellation as a user-visible contract
- Downstream OBO after worker replacement
- Approval resume after replacement with only the authorized principal
- Removing ChatAgent routes from `_wiring.create_app` once every remaining
  caller uses `/api/invocations`

Do not treat `test_durable_recovery.py` or a `/readyz` `durable: true` deploy
as those proofs. Do not retry parked app `agent-bricks-stu-dur-1002` (#863).

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
| #858 | `apx memory extract` / `_memory_extract.py` | GitHub text still says extraction is unshipped |
| #864 | Consumer freeze + stop/start SNAPSHOT rollover | Session/invocation HTTP after worker loss (needs Apps OAuth, not PAT) |
| #575 | Wait | UC Agent Services GA |
| #909 | Agent Bricks forces `agent-bricks-*` | MCP `mcp-` discovery; workaround is App Space `apps_api` |
| #911 | Doctor names services that need a UI guardrail check | Attachment is UI-only |
| #946 / #947 | #947: templating + partition shipped; ConversationStore scoped methods rejected | #946 TS vs Python client; #947 durable `user:`/`app:` backend |
| #954 | Checks, deploy, doctor, native launcher, and `config_from_agent` read Python constructor settings when there is no `AgentConfig` | `[tool.apx.agent]` remains valid until sub-project 3 removes it |
| #836 | `session_budget={"usd": D}` on served turns, priced from usage metadata | Live billing vs published FMAPI list prices |
