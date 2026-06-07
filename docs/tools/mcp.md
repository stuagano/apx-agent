# MCP — Databricks Managed MCP

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

## Per-app MCP (Apps mode, legacy)

When you need a custom MCP server hosted from a Databricks App (custom tools that aren't UC functions, agent-as-MCP-target endpoints), Apps-hosted agents still expose `/mcp` (streamable HTTP transport). This was the right call before Managed MCP shipped and remains the only path for agents that have non-UC-resident tools you want to expose. For UC-resident assets, prefer Managed MCP.

```bash
# Apps-hosted: http://localhost:8000/mcp  or  https://<app>.databricksapps.com/mcp
```

Model Serving agents don't expose `/mcp` — Model Serving has a fixed `/invocations` endpoint with the `ChatAgent` contract.
