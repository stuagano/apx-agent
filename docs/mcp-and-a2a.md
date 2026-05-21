# MCP + A2A discovery + app-to-app auth

## MCP — Databricks Managed MCP

The modern path: every UC function, Genie space, and Vector Search index your agent declares is **automatically reachable as an MCP server** at the Databricks-hosted Managed MCP gateway. No per-app MCP route to deploy, no naming conventions for discovery, no app.yaml plumbing — the assets live in UC, and the gateway exposes them.

URL patterns the platform hosts:

| Kind | URL |
|------|-----|
| UC function | `https://<host>/api/2.0/mcp/functions/{catalog}/{schema}/{function}` |
| Genie space | `https://<host>/api/2.0/mcp/genie/{space_id}` |
| Vector Search index | `https://<host>/api/2.0/mcp/vector-search/{catalog}/{schema}/{index}` |

apx-agent generates these URLs and the corresponding client config from the agent's declared resources:

```python
from apx_agent import managed_mcp_urls, managed_mcp_client_config

endpoints = managed_mcp_urls(agent, workspace_host="https://my-workspace.cloud.databricks.com")
config = managed_mcp_client_config(endpoints, name="data-triage")

# config = {
#   "mcpServers": {
#     "data-triage.uc_function.main.tools.classify_intent": {
#       "type": "http",
#       "url": "https://my-workspace.cloud.databricks.com/api/2.0/mcp/functions/main/tools/classify_intent",
#       "oauth_scope": "unity-catalog"
#     },
#     "data-triage.genie_space.abc123": { ... },
#     ...
#   }
# }
```

Drop the result into Claude Desktop's `claude_desktop_config.json`, Cursor's `~/.cursor/mcp.json`, or any client that speaks the standard `mcpServers` shape. UC permissions are enforced end-to-end — the client authenticates as a user, the gateway calls UC, UC enforces the user's grants. No additional auth wiring.

### Per-app MCP (Apps mode, legacy)

When you need a custom MCP server hosted from a Databricks App (custom tools that aren't UC functions, agent-as-MCP-target endpoints), Apps-hosted agents still expose `/mcp` (streamable HTTP transport). This was the right call before Managed MCP shipped and remains the only path for agents that have non-UC-resident tools you want to expose. For UC-resident assets, prefer Managed MCP.

```bash
# Apps-hosted: http://localhost:8000/mcp  or  https://<app>.databricksapps.com/mcp
```

Model Serving agents don't expose `/mcp` — Model Serving has a fixed `/invocations` endpoint with the `ChatAgent` contract.

## A2A discovery

Apps-hosted agents publish `/.well-known/agent.json` with capabilities, skills, and MCP endpoint:

```json
{
  "name": "data_triage_agent",
  "description": "Investigate why data is missing from Databricks tables",
  "url": "https://data-triage-agent.workspace.databricksapps.com",
  "skills": [
    {"name": "get_table_lineage", "description": "Get upstream sources..."},
    {"name": "find_jobs_for_table", "description": "Which jobs write to a table..."}
  ],
  "mcpEndpoint": "https://data-triage-agent.workspace.databricksapps.com/mcp"
}
```

On Model Serving, UC + the Mosaic AI registry are the equivalent discovery surface.

## App-to-app authentication

When sub-agents are deployed as sibling Apps (not Model Serving endpoints), the orchestrator's calls go through the Databricks Apps SSO gateway:

1. **Routes under `/api/`** accept bearer tokens; other routes trigger interactive SSO. apx-agent mounts every endpoint at both its natural path and an `/api/` mirror.
2. **Each app has a service principal.** The platform creates one automatically. M2M credentials authenticate outbound calls.
3. **CAN_USE permission** on the callee app for the caller's SP. Without it, the gateway returns 401.
4. **FMAPI uses the app's own identity.** When app A calls app B, B's internal LLM calls use B's own SP token, not A's.

```bash
# 1. Mint an OAuth secret for each app's SP
databricks api post /api/2.0/accounts/servicePrincipals/<SP_ID>/credentials/secrets \
  --profile <profile> --json '{}'

# 2. Set credentials in each app.yaml
env:
  - name: DATABRICKS_CLIENT_ID
    value: "<app-sp-client-id>"
  - name: DATABRICKS_CLIENT_SECRET
    value: "<secret-from-step-1>"

# 3. Grant the orchestrator's SP CAN_USE on each sub-agent app
databricks api patch /api/2.0/permissions/apps/<sub-agent-name> \
  --profile <profile> --json '{
    "access_control_list": [{
      "service_principal_name": "<orchestrator-sp-client-id>",
      "permission_level": "CAN_USE"
    }]
  }'
```

**Common pitfalls:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| 302 redirect (HTML login page) | Calling `/responses` instead of `/api/responses` | Use `/api/` prefix for programmatic calls |
| 401 Unauthorized | Caller's SP lacks CAN_USE on callee | Grant via permissions API |
| FMAPI 401 inside sub-agent | Sub-agent using caller's OBO for LLM calls | Set `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` on the sub-agent |
| `invalid_client` on M2M | Wrong SP secret (app recreated, SP changed) | Mint a new secret for the current SP |

For user-scoped multi-agent across boundaries, prefer Model Serving deployment — the platform handles identity passthrough automatically, no SP-to-SP dance.
