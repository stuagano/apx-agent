# MCP — Databricks Managed MCP

Databricks Managed MCP is the platform gateway for supported Unity Catalog resources — UC
functions, Genie Agents, and AI Search indexes — as MCP servers accessible to MCP-compatible
clients. It removes the need to deploy a per-resource MCP server or add custom app routing, but
feature availability, OAuth scopes, resource existence, and Unity Catalog permissions still apply.

## Consuming MCP servers in your agent

To call tools from a remote MCP server inside your agent, use `mcp_tool` or `mcp_toolkit` from [custom-tools.md](custom-tools.md):

```python
from apx_agent import Agent, mcp_toolkit

agent = Agent(
    tools=mcp_toolkit("https://tools.example.com/mcp"),
)
```

For MCP servers in the same Databricks workspace, the calling user's OBO token is forwarded when
the request has a valid user-authorization context and the target supports that path.

---

## Using supported UC resources via Managed MCP

For supported UC resources, apx-agent can generate the Databricks-hosted Managed MCP endpoint
configuration. Declaring a resource in apx-agent does not provision the resource, enable a
preview feature, grant permissions, or guarantee that the endpoint is reachable. The assets live
in UC; the gateway applies the configured OAuth and UC authorization when the request runs.

URL patterns the platform hosts:

| Kind | URL |
|------|-----|
| UC function | `https://<host>/api/2.0/mcp/functions/{catalog}/{schema}/{function}` |
| Genie Agent | `https://<host>/api/2.0/mcp/genie/{space_id}` |
| AI Search index | `https://<host>/api/2.0/mcp/vector-search/{catalog}/{schema}/{index}` (legacy-compatible path) |

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

Drop the result into Claude Desktop's `claude_desktop_config.json`, Cursor's `~/.cursor/mcp.json`, or any client that speaks the standard `mcpServers` shape. UC permissions are enforced end-to-end when the client authenticates with the required scopes and the user has the required grants. No custom per-app MCP server is needed for these platform resources, but authorization setup is still required.

---

## Per-app MCP (Apps mode)

When you need a custom MCP server hosted from a Databricks App — for example, tools that aren't UC functions or agent-as-MCP-target endpoints — Apps-hosted agents expose `/mcp` (streamable HTTP transport). This is the right path for agents that have non-UC-resident tools you want to expose to external MCP clients. For UC-resident assets, prefer Managed MCP.

```bash
# Apps-hosted: http://localhost:8000/mcp  or  https://<app>.databricksapps.com/mcp
```

Apps-hosted agents always include `get_agent_flow_graph` on this MCP surface.
Use it when a client needs to inspect the live agent topology before deciding
which tool, handoff, or route evidence to use. It returns the compact
`/_apx/topology/digest` payload, not the full visual graph.

Model Serving agents don't expose `/mcp` — Model Serving uses the `/invocations` endpoint with the `ChatAgent` contract.
