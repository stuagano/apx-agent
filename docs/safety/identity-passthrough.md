# Identity passthrough

For every hop that supports user-token forwarding, the calling user's OAuth context flows through the agent's governed tool and data paths. Those calls run as the user — not as a shared service principal — and Unity Catalog enforces *their* grants on *their* data. This is not a universal claim about every outbound API or downstream model call: background/M2M paths and cross-app model calls remain app-scoped. No auth code is needed at the tool level; the framework handles capture, propagation, and resolution. See [the cross-app scope limit](#scope-limit-cross-app-model-calls).

In Databricks Apps, the user's OAuth token arrives as `X-Forwarded-Access-Token`. The framework captures it at the middleware boundary, propagates it through the async context, and resolves it at supported outbound calls.

```python
def get_table_lineage(table_full_name: str, ws: Dependencies.Workspace) -> dict:
    """Get upstream sources that feed into this table."""
    # ws is a per-request WorkspaceClient scoped to the calling user.
    # All Databricks API calls through ws run with that user's grants.
    rows = run_sql(ws, f"SELECT ... WHERE target = '{table_full_name}'")
    return {"target": table_full_name, "upstream_sources": rows}
```

## Resolution order

| Priority | Source | When it's used |
|----------|--------|---------------|
| 1 | Per-request OBO context | Interactive — user hits the app, their token flows through |
| 2 | Explicit headers | Caller passes auth directly (testing, manual invocation) |
| 3 | `DATABRICKS_TOKEN` env var | Local dev with a static PAT |
| 4 | M2M OAuth (`DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET`) | Background jobs, workflows — no user present |

When deployed to Databricks Apps and called from a trusted Databricks surface (Genie, or a Supervisor that routes to the App as a sub-agent) that supplies caller identity, the calling user's identity can thread through the agent and its sub-agents — scoped to the declared resources at every supported hop.

## New permissions require reauthorization

Consent is a lifecycle, not a grant: the first browser open of the App URL
is what mints the user's OBO token, so that first visit is the consent step.
Adding a `user_api_scope` later requires every user to re-consent before
their token carries it.

Adding a `user_api_scope` or a new governed resource does not guarantee that
an existing browser session will show a consent dialog. The caller's OBO token
may still be an older grant. If a request fails with `Invalid scope` or
`required scopes`, re-authorize the App by opening its URL and approving the
permissions prompt; if the prompt is cached, revoke the App authorization in
Genie (or clear the browser cookies) and open it again. Then retry the request.

This is an OAuth/session lifecycle constraint, not a substitute for declaring
the required scope in the bundle. The runtime surfaces the same recovery hint
for SQL, Genie, and model-serving scope denials.
Lakebase (Postgres) and the Files API run as the App service principal,
not the user's OBO token, so they have no per-user consent lifecycle.

## Apps URL inbound auth

Call a deployed Databricks App as an API with a user-to-machine or
machine-to-machine OAuth access token (`Authorization: Bearer …`). Use
`apps_inbound_oauth_headers(oauth_token=...)`. PAT cannot invoke an Apps URL
(SSO). Do not send `DATABRICKS_TOKEN` at that URL.

- **Getting a token.** U2M: `databricks auth login --host <workspace-url>
  --profile <name>`, then `databricks auth token --profile <name>`. M2M: a
  service principal's client credentials (`DATABRICKS_CLIENT_ID` +
  `DATABRICKS_CLIENT_SECRET`), e.g. `WorkspaceClient().config.authenticate()`
  in the Databricks SDK.
- **Not an A2A hop.** This is how an external caller (eval, job, script)
  reaches the Apps URL. It is separate from A2A agent-to-agent hops, which
  forward the caller's identity per hop.
- **The caller is the data principal, user or service principal.** The Apps
  proxy forwards a token for *whoever called*, in `X-Forwarded-Access-Token`,
  with `X-Forwarded-User` = the user, or `<sp-id>@<workspace-id>` for a service
  principal. The durable target treats both the same way: tools that run on
  behalf of the caller run under that principal's Unity Catalog grants, and its
  runs are isolated from other callers
  (`test_native_service_principal_caller_is_its_own_request_principal`).
  `CAN_USE` on the App only lets the caller through the front door; it grants
  nothing in UC. Across A2A hops trust stays per-hop user OBO; see
  [../design/a2a-trust-model.md](../design/a2a-trust-model.md).

### What the app receives

Observed on a deployed App, calling it with a user token and with a service
principal's M2M token:

| | User caller | Service-principal caller |
|---|---|---|
| `X-Forwarded-Access-Token` | yes | yes |
| Token `sub` / who it acts as downstream | the user | the service principal |
| Token scopes | **downscoped** to the App's `user_api_scopes` (an `all-apis` CLI token arrives with only the declared scopes) | same |
| Caller's `Authorization` header | stripped before the app | stripped before the app |
| No `CAN_USE` on the App | — | **HTTP 401** (not 403) |

Declare the scopes the tools need (for example `sql`) in `user_api_scopes`;
the forwarded token carries nothing else.

### Per-tenant principals through token federation

A multi-tenant caller (a gateway or another service, outside Databricks) can
reach the App as a **service principal per tenant** without storing any
Databricks secret:

1. Create one service principal per tenant, granted only that tenant's catalog.
2. On each, add a [service principal federation policy](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-federation-policy)
   that trusts your identity provider's issuer, requires an audience such as
   `databricks`, and matches a tenant claim (`subject_claim: "tenant_id"`,
   `subject: "<tenant>"`).
3. The caller [exchanges](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-federation-exchange)
   the end user's identity-provider JWT at `/oidc/v1/token` with
   `client_id=<tenant service principal>`, then calls the App with the
   resulting token.

The exchange succeeds only if the JWT is signed by that issuer, carries the
audience, and its tenant claim matches that service principal's policy. A JWT
for tenant B presented for tenant A's service principal is rejected
(`TOKEN_SUBJECT_INVALID`), as is a wrong audience (`TOKEN_AUDIENCE_INVALID`)
or an edited token (`TOKEN_INVALID`). Isolation is then enforced by UC grants,
not by app code. Trade-offs: access is per tenant, not per user. The docs
recommend `sub` as the subject claim, so matching a tenant claim is a
deliberate exception. Each service principal allows at most 20 federation policies.

## App service principal and ACLs

Each app has a dedicated service principal. Copy its application id from the
Databricks Apps Authorization tab (doctor prints it when the app exists) and
grant Unity Catalog on that principal, not a personal SP. Keep CAN MANAGE on
the App object small; grant CAN USE to users who should run the agent.

## Workspace OAuth-scope allowlist

Workspace admins can restrict Apps OBO scopes (`allowedAppsUserApiScopes`).
`apx-agent doctor` and deploy read that setting and fail when the plan asks
for a blocked scope.

## M2M federation

This is about the **app's own** principal. Workload identity federation without
a client secret is unsupported for it: the app principal path is injected
`DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` (`auth_type="oauth-m2m"`).
Do not fall back to `DATABRICKS_TOKEN` for that principal. Callers *into* the
App can use federation; see
[per-tenant principals](#per-tenant-principals-through-token-federation).

## Scope limit: cross-app model calls

On an A2A hop between two Databricks Apps, supported caller-token forwarding lets the callee's tools and governed data calls run as the asking user. The callee's own LLM (FMAPI) calls do not: they run as the callee app's service principal, because each App authenticates outbound model traffic with its own credentials. Data access can be user-scoped per hop; model access is app-scoped. See [../multi-agent/a2a.md](../multi-agent/a2a.md) for the full auth path.
