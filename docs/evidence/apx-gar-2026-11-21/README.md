# apx-gar: caller identity after replacement is blocked

**Observed 2026-11-21, UTC.** Operator authorization for the disposable app
was given on 2026-11-20. This bead did not deploy, did not kill a worker, and
did not send an invocation. [Sanitized record](evidence.json).

## Result

No live parent/child delegation was run. The unauthorized-principal case
cannot be exercised with the profiles already configured.

`databricks auth profiles -o json` returned **5** profiles. Exactly one is on
workspace `7474660401027952` (the disposable app's workspace):

| Profile | Host | auth_type | valid |
|---|---|---|---|
| `fevm-stable` | `https://fevm-serverless-stable-7def3z.cloud.databricks.com` | `databricks-cli` | true |

The other four profiles are workspace `1965859176160743` and were not used:

- `ug-oauth-dbc-a5d4177a-49dc.cloud.databricks.com-4f7af981-8977-4042-b90e-a5dea5b62e19` (default)
- `isaac-mcp-ai_devtools_prod.it_ex_internal_mcps.people-mcp`
- `isaac-mcp-ai_devtools_prod.it_ex_internal_mcps.perf-mcp`
- `isaac-mcp-ai_devtools_prod.it_ex_internal_mcps.brickroad-mcp`

`WorkspaceClient(profile="fevm-stable").config.authenticate()` reported
`auth_type=databricks-cli`. The auth type was not a personal access token.
The token was not printed. No principal was created. No personal access token
was minted. The app service principal was not used as a caller. A second user
was not borrowed to stand in for the unauthorized case.

`databricks apps get agent-bricks-apx-dur-gen -p fevm-stable -o json` at
`2026-11-21T01:07:28.868790+00:00` returned `app_status.state=RUNNING`,
message `App has status: App is running`, `running_instances=1`,
`compute_status.state=ACTIVE`, message `App compute is running.`,
deployment `01f1e546718010678017040e46a17107` mode `SNAPSHOT` status
`SUCCEEDED`, update time `2026-11-21T00:57:08Z`. HTTP invocations sent by
this bead: **0**. `databricks apps stop` / `start` was not called.

## Symbols inspected

Installed `databricks-agentbricks` **0.10.0**
(`databricks_agentkit/runtime/app.py`, local reader tree, not a new deploy):

- `DurableAgentServer._execute`, when `InvocationAuthPolicy.requires_user`
  and `attempt_context.is_recovery`, raises
  `AuthError("MCP_USER_AUTH_RECOVERY_UNSUPPORTED", "Request-user execution does not survive failure recovery yet", 500)`.
- The same code and message are raised when request auth is missing on that
  path.
- `RequestAuthContext.from_headers` reads `x-forwarded-access-token` and
  `x-forwarded-user`. Missing token is `MCP_USER_AUTH_REQUIRED`. Missing
  principal is `MCP_USER_IDENTITY_MISSING`.
- `apx_agent._durable_agent` refuses the combination at compile time:
  `ValueError: recovery is incompatible with the manifest's request-user authentication`
  when `recovery and policy.requires_user`.

Those symbols are why a request-user crash cannot be recovered by
reconstructing credentials. They are not a substitute for the missing second
principal. SP-to-SP reachability was not treated as caller authorization.
GitHub [#816](https://github.com/stuagano/apx-agent/issues/816) and
[#755](https://github.com/stuagano/apx-agent/issues/755) stay coordination
items; this session did not prove per-hop scopes.

## Versions

Databricks CLI **v1.26.0**. Local reader `databricks-sdk` **0.146.0**.
Inspected `databricks-agentbricks` **0.10.0**. No gar snapshot was deployed,
so there is no in-app version for this bead.

## Acceptance left open

Parent and child invocation IDs, downstream event readback, UC/audit of the
actual caller, authorized continuation after replacement, and an unauthorized
or missing-credential failure after replacement are not recorded. Compatibility
routes and custom runtime machinery were not removed. The app was left
running.
