# apx-ab7: approval continuation after replacement is blocked

**Observed 2026-11-21, UTC.** Operator authorization for the disposable app
was given on 2026-11-20. This bead did not deploy, did not create an approval,
and did not send an invocation. [Sanitized record](evidence.json).

## Result

No pending approval was parked across a worker replacement. The unauthorized
principal required by the acceptance is not configured.

`databricks auth profiles -o json` returned **5** profiles. Exactly one is on
workspace `7474660401027952`:

| Profile | Host | auth_type | valid |
|---|---|---|---|
| `fevm-stable` | `https://fevm-serverless-stable-7def3z.cloud.databricks.com` | `databricks-cli` | true |

The other four profiles are workspace `1965859176160743` and were not used:

- `ug-oauth-dbc-a5d4177a-49dc.cloud.databricks.com-4f7af981-8977-4042-b90e-a5dea5b62e19` (default)
- `isaac-mcp-ai_devtools_prod.it_ex_internal_mcps.people-mcp`
- `isaac-mcp-ai_devtools_prod.it_ex_internal_mcps.perf-mcp`
- `isaac-mcp-ai_devtools_prod.it_ex_internal_mcps.brickroad-mcp`

`WorkspaceClient(profile="fevm-stable").config.authenticate()` at
`2026-11-21T01:13:22.936841+00:00` reported `auth_type=databricks-cli`. The
auth type was not a personal access token. The token was not printed. No
principal was created. No personal access token was minted. The app service
principal was not used as a caller.

`databricks apps get agent-bricks-apx-dur-gen -p fevm-stable -o json` at
`2026-11-21T01:12:33.386430+00:00` returned `app_status.state=RUNNING`,
message `App has status: App is running`, `running_instances=1`,
`compute_status.state=ACTIVE`, message `App compute is running.`,
deployment `01f1e546718010678017040e46a17107` mode `SNAPSHOT` status
`SUCCEEDED`, update time `2026-11-21T00:57:08Z`. HTTP invocations sent by
this bead: **0**. `databricks apps stop` / `start` was not called.

The serving snapshot is the earlier effect probe. A new non-recovery user
message on that snapshot exits the worker. This bead did not send one, and
did not replace the snapshot with an approval probe. A single-principal run
would not show that the wrong principal is denied.

## Symbols inspected

Installed `databricks-agentbricks` **0.10.0**
(`databricks_agentkit/runtime/app.py`):

- `DurableAgentServer._execute` raises
  `AuthError("MCP_USER_AUTH_RECOVERY_UNSUPPORTED", "Request-user execution does not survive failure recovery yet", 500)`
  when `InvocationAuthPolicy.requires_user` and the attempt is a recovery.
- `RequestAuthContext.from_headers` is the request-user credential. It is not
  stored on the invocation.

APX source on this worktree:

- `build_native_manifest` raises `ValueError("Request-user recovery is unsupported")`
  when recovery is declared and request-user auth is required.
- `compile_durable_handlers.request_auth` raises the same `ValueError` when
  recovery is on and a `RequestAuthContext` is present.
- `PolicyGate` pauses a checkpointed tool call through
  `_interrupt_for_approval`, which calls `langgraph.types.interrupt`.
- `ApprovalStore` in `apx_agent._policy` is an in-memory registry. It is not
  itself a store that survives a process exit.
- A later turn resumes with `Command(resume=resume)` in `apx_agent._graph_turn`.
  `_validate_native_input` raises
  `ValueError("Approval resume requires a checkpointed session")` when
  `resume` is set and the session is not checkpointed.

Those symbols were read. They were not exercised on the deployed app. A local
service-identity approval test is not this acceptance. Custom approval
machinery was not removed.

## Versions

Databricks CLI **v1.26.0**. Local reader `databricks-sdk` **0.146.0**.
Inspected `databricks-agentbricks` **0.10.0**. No ab7 snapshot was deployed.

## Acceptance left open

There is no session id, original invocation id, replacement id, or resume
invocation id. Unauthorized continuation, one authorized continuation,
duplicate-resume rejection, and checkpoint or audit readback after replacement
were not observed. The app was left running.
