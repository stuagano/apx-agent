# apx-agent

Agent toolkit for Databricks Apps — typed tools, MCP exposure, A2A discovery, registry auto-registration, and dev UI.

## Where apx-agent fits

Databricks offers multiple approaches to multi-agent orchestration. apx-agent targets the middle ground: more structured than "write your own LangGraph" but more controllable than "hope the LLM picks the right tool."

| Need | Solution | Routing | Auth | Custom logic |
|------|----------|---------|------|-------------|
| Simple multi-tool, LLM-routed | **Supervisor API** (native) | Probabilistic — LLM picks tools based on descriptions | OBO automatic | Instructions only |
| Deterministic routing, conditional logic, workflows | **apx-agent** | Deterministic — developer controls flow | OBO via headers + DatabricksOpenAI | Full Python control |
| Full graph-based orchestration | **LangGraph** custom agent | Developer-defined edges and conditions | Manual | Full Python, graph DSL |

### Supervisor API (Beta)

The [Supervisor API](https://docs.databricks.com/aws/en/generative-ai/agent-bricks/supervisor-api) runs the agent loop server-side. You send a model + tools + input, Databricks handles everything:

```python
from databricks_openai import DatabricksOpenAI
client = DatabricksOpenAI(use_ai_gateway=True)

response = client.responses.create(
    model="databricks-claude-sonnet-4-5",
    input=[{"type": "message", "role": "user", "content": "..."}],
    tools=[
        {"type": "genie_space", "genie_space": {"space_id": "...", "description": "..."}},
        {"type": "unity_catalog_function", "unity_catalog_function": {"name": "...", "description": "..."}},
        {"type": "external_mcp_server", "external_mcp_server": {"connection_name": "...", "description": "..."}},
    ],
)
```

**Strengths:** Fully OBO (user token flows through entire chain — no SP credentials needed), server-managed tool execution, model swappable per request, AI Gateway integration with tracing.

**Limitations:** 100% probabilistic routing (LLM decides which tool to call based on descriptions), no conditional logic, no deterministic workflows, no custom business logic, cannot mix server-side and client-side tools, max 20 tools.

### When to use apx-agent instead

Use apx-agent when you need:

- **Guaranteed routing** — "billing questions always go to the bill agent, never to triage"
- **Investigation workflows** — "check data → trace lineage → inspect jobs → read source → synthesize" in that order
- **Conditional logic** — "if the table has lineage, trace it; if not, check job history directly"
- **Custom tool execution** — tools that need local state, complex error handling, or multi-step orchestration
- **MCP server exposure** — agents that other tools (Claude Desktop, Cursor, Genie Code) connect to
- **A2A discovery** — agents that publish capabilities at `/.well-known/agent.json` and self-register with a hub

### Interoperability

apx-agent apps are standard Databricks Apps. They can be consumed by Supervisor Agent as MCP servers (`/mcp` endpoint) or as agent endpoints. The same agent works in both worlds:

- **Standalone** — serves its own chat UI, MCP endpoint, and `/invocations` API
- **As a Supervisor sub-agent** — Supervisor calls the app's MCP endpoint with OBO auth
- **As a DatabricksOpenAI target** — other agents call via `model="apps/<app-name>"` using the Responses API

### Relationship to OpenAI Agents SDK

The [Databricks app templates](https://github.com/databricks/app-templates) use the OpenAI Agents SDK (`from agents import Agent, Runner`) for the agent loop. apx-agent provides complementary capabilities that the OpenAI SDK doesn't have:

| Capability | OpenAI Agents SDK | apx-agent |
|-----------|-------------------|-----------|
| LLM loop + tool dispatch | `Runner.run()` | Custom loop (to be migrated to Runner) |
| Agent-as-tool | `agent.as_tool()` | Sub-agent URL composition |
| Handoffs | `Agent(handoffs=[...])` | Not yet |
| Typed function → tool schema | `@function_tool` | `Agent(tools=[fn])` with type hint inspection |
| MCP server exposure | Not built-in | `/mcp/sse` + `/mcp` (streamable HTTP) |
| A2A discovery card | Not built-in | `/.well-known/agent.json` |
| Registry auto-registration | Not built-in | `registry` config in pyproject.toml |
| Dev UI (chat + tool inspector) | Not built-in | `/_apx/agent` + `/_apx/tools` |
| SQL utilities | Not built-in | `Dependencies.Sql`, `run_sql()` |
| Dependency injection | Not built-in | `Dependencies.Client`, `.Workspace`, `.Headers` |
| Sequential / Parallel composition | Not built-in | Planned (ADK-style workflow agents) |

**Future direction:** Migrate the LLM loop to use `DatabricksOpenAI.responses.create()` and OpenAI Agents SDK `Runner.run()`, keeping apx-agent as the DX/ops layer (discovery, registry, MCP, dev UI, workflow composition) on top of the official runtime.

## Quick start

```python
from apx_agent import Agent, Dependencies, create_app

def get_billing(customer_id: str, ws: Dependencies.Workspace) -> dict:
    """Get billing history."""
    ...

agent = Agent(tools=[get_billing])
app = create_app(agent)
```

```bash
uvicorn my_app:app --reload
```

## Features

### Typed tool registration

Define tools as plain Python functions. Type hints become the input schema, the docstring becomes the description. `Dependencies.*` parameters are injected by FastAPI and excluded from the schema.

```python
def get_table_lineage(table_full_name: str, ws: Dependencies.Workspace) -> dict:
    """Get upstream sources that feed into this table via Unity Catalog lineage."""
    rows = run_sql(ws, f"SELECT ... FROM system.access.table_lineage WHERE target = '{table_full_name}'")
    return {"target": table_full_name, "upstream_sources": rows}
```

### A2A discovery

Every agent exposes `/.well-known/agent.json`:

```json
{
  "name": "data_triage_agent",
  "description": "Investigate why data is missing from Databricks tables or APIs",
  "url": "https://data-triage-agent.workspace.databricksapps.com",
  "skills": [
    {"name": "get_table_lineage", "description": "Get upstream sources..."},
    {"name": "find_jobs_for_table", "description": "Which jobs write to a table..."}
  ],
  "mcpEndpoint": "https://data-triage-agent.workspace.databricksapps.com/mcp"
}
```

### Registry auto-registration

Agents self-register with a hub on startup:

```toml
# pyproject.toml
[tool.apx.agent]
name = "data_triage_agent"
description = "Investigate missing data"
model = "databricks-claude-sonnet-4-6"
url = "$DATA_TRIAGE_AGENT_URL"
registry = "$AGENT_HUB_URL"
```

### Sub-agent composition

Agents can call other agents deployed as Databricks Apps:

```python
agent = Agent(
    tools=[get_table_lineage, find_jobs_for_table],
    sub_agents=["$DATA_INSPECTOR_URL"],
    instructions="Use the data_inspector for SQL queries and Delta forensics.",
)
```

### MCP server

Every agent exposes MCP at `/mcp/sse` (SSE transport) and `/mcp` (streamable HTTP). Connect from Claude Desktop, Cursor, Genie Code, or Supervisor Agent.

### Dev UI

- `/_apx/agent` — chat interface for testing
- `/_apx/tools` — tool inspector with live invocation forms
- `/_apx/probe?url=<url>` — outbound connectivity tester

## Configuration

All config lives in `[tool.apx.agent]` in `pyproject.toml`:

```toml
[tool.apx.agent]
name = "my_agent"
description = "What this agent does"
model = "databricks-claude-sonnet-4-6"
instructions = "System prompt for the agent"
max_iterations = 10
sub_agents = ["$OTHER_AGENT_URL"]
url = "$MY_AGENT_URL"
registry = "$AGENT_HUB_URL"
```

Environment variable references (`$VAR` or `${VAR}`) are resolved at startup.
