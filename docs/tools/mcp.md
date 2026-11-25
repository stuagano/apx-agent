# MCP — Unity Gateway MCP services and workspace Managed MCP

Databricks hosts MCP servers two ways:

| | Unity Gateway MCP services | Workspace Managed MCP |
|---|---|---|
| URL | `https://<host>/ai-gateway/mcp-services/<catalog>.<schema>.<service>` | `https://<host>/api/2.0/mcp/...` |
| OAuth scope | `ai-gateway` | per server (`genie`, `ai-search`, `sql`, `unity-catalog`) |
| Governance | Unity Gateway grants, policies, and tracing | Permissions on the underlying resource only. **Bypasses Unity Gateway** (no UG tracing, MCP grants, policies, or guardrails) |
| Status | Unity Gateway is GA; per-service status below | Public Preview |
| Enable | Account admin: **Enhanced Unity Gateway** (Beta services) | Workspace admin: **Managed MCP Servers** (workspace Previews page) |
| apx factory | `unity_gateway_mcp(service)` | `mcp_tool` / `mcp_toolkit` with the URL, `genie_one_tool()`, `managed_mcp_urls(...)` |

Databricks recommends the Unity Gateway `system.ai` services for new integrations when they
cover the use case. The workspace endpoints remain useful for resource-scoped access (one Genie
Agent, one AI Search index, one schema of UC functions).

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

## Unity Gateway MCP services (`system.ai.*`)

`unity_gateway_mcp(service)` wraps a Databricks-provided MCP service's tools as agent tools:

```python
from apx_agent import Agent, sandbox_tool, unity_gateway_mcp

agent = Agent(tools=[
    *unity_gateway_mcp("system.ai.web_search"),
    *unity_gateway_mcp("system.ai.dbsql", tools=["execute_sql"]),
    *unity_gateway_mcp("system.ai.github"),
])
```

- Every call goes to `{caller's workspace}/ai-gateway/mcp-services/<service>` as the **calling
  user** (OBO), so Unity Gateway grants, policies, and tracing apply to that user.
- Each tool declares the `ai-gateway` user API scope (written to `databricks.yml` on deploy) and a
  `ServiceBinding(service, "user")`. There is no Apps resource for these services, so the binding
  adds nothing else to `databricks.yml`.
- With `tools=[...]`, no network call is made when the agent is built; the model passes the
  remote tool's arguments as one `arguments` dict. Without it, the service's tools and
  descriptions are listed once with the default workspace client (the App service principal in
  Apps, your profile locally), which needs access to the service.
- Local tool names are `<service>_<remote tool>` (for example `web_search_search`), so they never
  collide with each other or with `sandbox_tool`'s `run_in_sandbox`.
- `unity_gateway_mcp_url(service, workspace_host=...)` returns the full URL for MCP clients
  outside apx.

| Service | Status | What it does | Needs |
|---|---|---|---|
| `system.ai.genie_one_mcp` | GA | Workspace-wide natural-language analytics | Grant on the service |
| `system.ai.dbsql` | Beta | Run a specific SQL query as the caller | Enhanced Unity Gateway; warehouse and data grants |
| `system.ai.web_search` | Beta | Public web search with citations | Enhanced Unity Gateway |
| `system.ai.sandbox` | Beta | Run Python or shell in an isolated environment | Enhanced Unity Gateway; Databricks Sandbox |
| `system.ai.slack`, `github`, `gmail`, `google_drive`, `google_calendar`, `microsoft_365`, `atlassian`, `asana` | GA connectors | Per-user OAuth to those apps | Each user signs in to the provider; the provider's org admin may need to approve the Databricks app |

Enable steps:

1. Account admin: enable **Enhanced Unity Gateway** on the account console Previews page
   (required for `dbsql`, `sandbox`, `web_search`).
2. Grant the calling users access to the MCP service and to the underlying data or warehouse.
3. For connectors, each user signs in to the provider with their own account the first time.

`apx-agent doctor` looks up every declared service
(`GET /api/2.1/unity-catalog/mcp-services/<service>`) with the checking identity and prints these
steps when a service is unavailable or unreadable. It does not prove the runtime identity can
call the service.

### `system.ai.sandbox` and `sandbox_tool`

[`sandbox_tool`](overview.md#code-execution--sandbox_tool) (#1266) calls the Sandbox REST API and
owns the sandbox lifecycle (throwaway or named sandboxes). `unity_gateway_mcp("system.ai.sandbox")`
(#1268) calls the same service as a Unity Gateway MCP, governed by UG grants and policies. Both
declare `ServiceBinding("system.ai.sandbox", "user")`, so an agent using both has one binding, and
their tool names do not overlap. Use `sandbox_tool(auth="app")` to run as the App service
principal; `unity_gateway_mcp` always runs as the caller.

### `system.ai.genie_one_mcp` and `genie_one_tool`

`genie_one_tool()` calls Genie One through the workspace Managed MCP endpoint
`/api/2.0/mcp/genie` (scope `genie`, bypasses Unity Gateway) and owns the ask-and-poll loop.
`unity_gateway_mcp("system.ai.genie_one_mcp")` is the Unity Gateway-governed alternative; it
exposes the raw tools, so the model drives polling itself. `genie_one_tool()` keeps its current
path.

---

## Workspace Managed MCP (Public Preview, bypasses Unity Gateway)

For supported UC resources, apx-agent can generate the Databricks-hosted Managed MCP endpoint
configuration. Declaring a resource in apx-agent does not provision the resource, enable a
preview feature, grant permissions, or guarantee that the endpoint is reachable. A workspace admin
enables **Managed MCP Servers** on the workspace Previews page; `apx-agent doctor` lists the
declared tools that call `/api/2.0/mcp/...` and names that preview.

URL patterns the platform hosts:

| Kind | URL | OAuth scope |
|------|-----|-------------|
| UC function | `https://<host>/api/2.0/mcp/functions/{catalog}/{schema}/{function}` | `unity-catalog` |
| Genie Agent | `https://<host>/api/2.0/mcp/genie/{space_id}` | `genie` |
| Genie One | `https://<host>/api/2.0/mcp/genie` — asynchronous (`genie_ask` then `genie_poll_response`, typically 40–260s); use `genie_one_tool()` so polling stays out of the model's step budget | `genie` |
| AI Search index | `https://<host>/api/2.0/mcp/ai-search/{catalog}/{schema}/{index}` | `ai-search` |
| Databricks SQL | `https://<host>/api/2.0/mcp/sql` | `sql` |

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

### Slow and asynchronous MCP tools

`mcp_tool(...)` and `mcp_toolkit(...)` take `timeout_s=` (per request, default 30s) for tools that legitimately run long.

For servers that answer a slow call with `status: "in_progress"` plus ids and expect polling, give `mcp_tool` the poll tool so the model makes one call:

```python
ask = mcp_tool(f"{host}/api/2.0/mcp/genie", "genie_ask",
               poll_tool="genie_poll_response", poll_ids=("conversation_id", "response_id"),
               wait_s=150)
```

The loop (`call_until_done`) polls every `poll_s` until the status changes or `wait_s` passes, then returns the last result. Some servers document a streaming mode when the client sends an MCP progress token; Genie One currently answers `in_progress` either way, so apx polls.

---

## Per-app MCP (Apps mode)

When you need a custom MCP server hosted from a Databricks App — for example, tools that aren't UC functions or agent-as-MCP-target endpoints — Apps-hosted agents expose `/mcp` (streamable HTTP transport). This is the right path for agents that have non-UC-resident tools you want to expose to external MCP clients. For UC-resident assets, prefer Unity Gateway MCP services or Managed MCP.

```bash
# Apps-hosted: http://localhost:8000/mcp  or  https://<app>.databricksapps.com/mcp
```

Apps-hosted agents always include `get_agent_flow_graph` on this MCP surface.
Use it when a client needs to inspect the live agent topology before deciding
which tool, handoff, or route evidence to use. It returns the compact
`/_apx/topology/digest` payload, not the full visual graph.

### Restricting a tool to specific callers

Every tool on `/mcp` is visible to every caller by default. To limit one, declare
`ToolScope.callers` — the emails allowed to use it — on the Python callable:

```python
# tools/billing.py
from apx_agent import ToolScope, build_tool, uc_function_tool

create_refund = uc_function_tool("main.billing.refund_create")
create_refund = build_tool(
    create_refund,
    name="create_refund",
    description="Create a billing refund for an authorized caller.",
    scope=ToolScope(callers=("alice@example.com", "billing-bot@example.com")),
)
```

Import `create_refund` in `agent.py` and include it in the tool list. `build_tool`
annotates the same callable and retains the factory's resource declarations;
`callers` is not a kwarg on `uc_function_tool`. See the
[Python-native layout and migration](overview.md#python-native-tool-layout).

The caller is the Databricks Apps `X-Forwarded-Email`. MCP `list_tools` hides the
tool from anyone else, and its `/api/tools/<name>` route (which MCP `call_tool`
dispatches through) returns 403 `caller_denied` with a `scope_action=deny` audit
event. A request with no caller email is denied. Tools without `callers` stay
open. Matching is by email only; group membership is not resolved.
