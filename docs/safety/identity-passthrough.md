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

When deployed to Model Serving and called from a trusted Databricks surface (AI Playground, Genie, Review App, or Supervisor sub-agent) that supplies caller identity, the calling user's identity can thread through the agent and its sub-agents — scoped to the declared resources at every supported hop.

## New permissions require reauthorization

Adding a `user_api_scope` or a new governed resource does not guarantee that
an existing browser session will show a consent dialog. The caller's OBO token
may still be an older grant. If a request fails with `Invalid scope` or
`required scopes`, re-authorize the App by opening its URL and approving the
permissions prompt; if the prompt is cached, revoke the App authorization in
Genie (or clear the browser cookies) and open it again. Then retry the request.

This is an OAuth/session lifecycle constraint, not a substitute for declaring
the required scope in the bundle. The runtime surfaces the same recovery hint
for SQL scope denials.

## Scope limit: cross-app model calls

On an A2A hop between two Databricks Apps, supported caller-token forwarding lets the callee's tools and governed data calls run as the asking user. The callee's own LLM (FMAPI) calls do not: they run as the callee app's service principal, because each App authenticates outbound model traffic with its own credentials. Data access can be user-scoped per hop; model access is app-scoped. See [../multi-agent/a2a.md](../multi-agent/a2a.md) for the full auth path.
