# Identity passthrough

The calling user's OAuth token flows through every tool, every sub-agent call, and every outbound Databricks API call. The agent's data access runs as the user — not as a service principal. Unity Catalog enforces *their* grants on *their* data. No auth code at the tool level; the framework handles capture, propagation, and resolution. (One scope limit applies to cross-app model calls — see [below](#scope-limit-cross-app-model-calls).)

In Databricks Apps, the user's OAuth token arrives as `X-Forwarded-Access-Token`. The framework captures it at the middleware boundary, propagates it through the async context, and resolves it at every outbound call.

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

When deployed to Model Serving and called from a trusted Databricks surface (AI Playground, Genie, Review App, Supervisor sub-agent), the calling user's identity threads automatically through the agent and its sub-agents — scoped to the declared resources at every hop.

## Scope limit: cross-app model calls

On an A2A hop between two Databricks Apps, the caller's OBO token is forwarded, so the callee's tools and Databricks API calls still run as the asking user. The callee's own LLM (FMAPI) calls do not: they run as the callee app's service principal, because each App authenticates outbound model traffic with its own credentials. Data access is user-scoped per hop; model access is app-scoped. See [../multi-agent/a2a.md](../multi-agent/a2a.md) for the full auth path.
