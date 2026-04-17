# apx-agent

A standard set of tools for building AI agents on Databricks Apps. Available in **Python** and **TypeScript**.

## Why this exists

The hardest problem when building agents on Databricks Apps isn't the LLM loop — it's **auth**.

Databricks Apps solves *inbound* auth beautifully. When a user hits your app, the platform validates their OAuth token and injects it into your request as `X-Forwarded-Access-Token`. But the platform has no visibility into your app's *outbound* calls. When your tool handler calls the Genie API, runs a SQL statement, queries Unity Catalog, or talks to another agent app — that's just a regular HTTP call from inside a Node or Python process. Nothing automatically threads the user's token to it.

So your tools silently run as the **app's service principal** instead of the **calling user**. This means either too much access (governance violation) or too little (the user's data isn't reachable). Every developer has to manually extract the OBO header, pass it through every function call, and attach it to every outbound `fetch`. Most don't — and the ones who try get it wrong more often than not.

apx-agent fixes this at the framework level:

1. **Extracts** the user's OBO token from every inbound request
2. **Stores** it in per-request async context (`AsyncLocalStorage` in TS, FastAPI dependency injection in Python)
3. **Provides** a single `resolveToken()` that every tool, connector, and sub-agent call uses automatically

```typescript
// You write this:
const tool = genieTool('abc123');

// The framework handles this:
//   inbound request → extract X-Forwarded-Access-Token → store in async context
//   tool handler runs → resolveToken() reads from context → outbound call uses user's token
//   user's Genie permissions apply, not the service principal's
```

Every built-in tool factory (`genieTool`, `catalogTool`, `lineageTool`, `schemaTool`, `ucFunctionTool`), every connector (`createLakebaseQueryTool`, `createVSQueryTool`), every external MCP client call, and every sub-agent invocation forwards the user's token automatically. Custom tools get it too — just call `resolveToken()` from `appkit-agent`.

**This is the #1 pain point** customers hit when building agents on Databricks Apps, and it's the core reason apx-agent exists. Everything else — typed tools, MCP server, A2A discovery, workflow agents — is built on top of this foundation.

## Quick start

### Python

```python
from apx_agent import Agent, create_app, lineage_tool, genie_tool

agent = Agent(
    tools=[
        lineage_tool(),
        genie_tool("abc123", description="Answer data questions"),
    ],
    instructions="You investigate missing data.",
)
app = create_app(agent)
```

```bash
cd python
uv sync
uvicorn my_app:app --reload
```

### TypeScript

```typescript
import { createApp, server } from '@databricks/appkit';
import { createAgentPlugin, createDiscoveryPlugin, createMcpPlugin, createDevPlugin, lineageTool, genieTool } from 'appkit-agent';

createApp({
  plugins: [
    server(),
    createAgentPlugin({
      model: 'databricks-claude-sonnet-4-6',
      instructions: 'You investigate missing data.',
      tools: [
        lineageTool(),
        genieTool('abc123', { description: 'Answer data questions' }),
      ],
    }),
    createDiscoveryPlugin({ registry: '$AGENT_HUB_URL' }),
    createMcpPlugin(),
    createDevPlugin(),
  ],
});
```

```bash
cd typescript
npm install
npm run dev
```

## Features

### Typed tools

Define tools as functions with type annotations. The framework generates input schemas and descriptions automatically.

**Python** — type hints + docstrings, with `Dependencies.*` parameters injected by FastAPI:

```python
def get_table_lineage(table_full_name: str, ws: Dependencies.Workspace) -> dict:
    """Get upstream sources that feed into this table via Unity Catalog lineage."""
    rows = run_sql(ws, f"SELECT ... FROM system.access.table_lineage WHERE target = '{table_full_name}'")
    return {"target": table_full_name, "upstream_sources": rows}
```

**TypeScript** — Zod schemas + handler functions:

```typescript
const getLineage = defineTool({
  name: 'get_table_lineage',
  description: 'Get upstream sources for a table',
  parameters: z.object({ tableName: z.string() }),
  handler: async ({ tableName }) => { /* ... */ },
});
```

### Platform tool factories

Pre-built tool factories for common Databricks platform capabilities. One line to register, no schema to write.

| Factory | What it does |
|---------|-------------|
| `genie_tool(space_id)` | Ask a natural-language question to a Genie space |
| `lineage_tool()` | Get upstream/downstream lineage for a UC table |
| `schema_tool()` | Describe columns of a UC table |
| `catalog_tool(catalog, schema)` | List tables in a UC schema |
| `uc_function_tool(function_name)` | Execute a registered UC function |

**`uc_function_tool` is a particularly strong unlock.** UC functions are already how data teams write and govern business logic — they define parameter types, write documentation, and apply access controls through standard UC governance. Without this, an AI engineer has to duplicate all of that work by hand-writing a tool schema and calling implementation that mirrors what the data team already registered. The two definitions then drift apart over time.

With `uc_function_tool`, the UC function *is* the tool definition. The data team owns the logic; the AI engineer registers it in one line. Governance, access control, and documentation flow through UC the same way they do for any other data asset. Data teams can ship new agent capabilities through their normal workflow — write SQL or Python, register in UC, done — without touching agent code.

```python
# Python — fetches parameter schema from UC on first call, builds SQL automatically
from apx_agent import Agent, uc_function_tool

agent = Agent(tools=[
    uc_function_tool("main.tools.classify_intent"),
    uc_function_tool("main.tools.score_customer"),
])
```

```typescript
// TypeScript — same pattern, auto-discovers SQL warehouse
import { ucFunctionTool } from 'appkit-agent';

createAgentPlugin({
  tools: [
    ucFunctionTool('main.tools.classify_intent'),
    ucFunctionTool('main.tools.score_customer'),
  ],
});
```

### Workflow agents

Composable agent patterns for multi-step orchestration:

| Agent | Purpose |
|-------|---------|
| **SequentialAgent** | Pipeline execution (analyze → plan → execute) |
| **ParallelAgent** | Fan-out/gather (fetch weather + news concurrently) |
| **LoopAgent** | Iterative refinement (draft → review → revise until done) |
| **RouterAgent** | Conditional routing (billing → bill agent, data → triage agent) |
| **HandoffAgent** | Peer handoff (triage → billing mid-conversation) |
| **RemoteAgent** | Cross-service agent communication |
| **EvolutionaryAgent** | Population-based search with Pareto selection |

### MCP server

Every agent exposes MCP at `/mcp/sse` (SSE transport) and `/mcp` (streamable HTTP). Connect from Claude Desktop, Cursor, Genie Code, or Supervisor Agent.

### A2A discovery

Every agent publishes `/.well-known/agent.json` with its capabilities, skills, and MCP endpoint:

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

### Sub-agent composition

Agents can call other agents deployed as Databricks Apps:

**Python:**
```python
agent = Agent(
    tools=[get_table_lineage, find_jobs_for_table],
    sub_agents=["$DATA_INSPECTOR_URL"],
    instructions="Use the data_inspector for SQL queries and Delta forensics.",
)
```

**TypeScript:**
```typescript
agent({
  tools: [getLineage],
  subAgents: ['$DATA_INSPECTOR_URL'],
  instructions: 'Use the data_inspector for SQL queries.',
})
```

### Registry auto-registration

Agents self-register with a hub on startup:

```toml
# python/pyproject.toml
[tool.apx.agent]
name = "data_triage_agent"
description = "Investigate missing data"
model = "databricks-claude-sonnet-4-6"
registry = "$AGENT_HUB_URL"
```

### Dev UI

Built-in development tools at:
- `/_apx/agent` — chat interface for testing
- `/_apx/tools` — tool inspector with live invocation
- `/_apx/probe?url=<url>` — outbound connectivity tester

## How it fits

Databricks provides two excellent native paths for building agents:

- **Mosaic AI Agent Framework** (`ResponsesAgent` + MLflow) — agents logged as MLflow models, deployed as Model Serving endpoints, with built-in tracing, evaluation, and a bundled Chat UI.
- **Supervisor API** — a managed orchestration layer where an LLM routes to sub-agents via MCP.

apx-agent extends these with a third path: **agents deployed as Databricks Apps** with **developer-controlled orchestration**. Apps are long-running FastAPI-style services, which opens up patterns that complement the platform — custom UIs, stateful workflows, direct Databricks SDK access with per-user auth, and agent-to-agent composition across app boundaries.

| Need | Solution | Routing |
|------|----------|---------|
| Simple multi-tool, LLM-routed | **Supervisor API** (native) | Probabilistic — LLM picks tools |
| Deterministic routing, workflows, custom logic | **apx-agent** | Developer-controlled |
| Full graph-based orchestration | **LangGraph** | Developer-defined graph |

apx-agent apps work standalone, as Supervisor sub-agents (via MCP), or as DatabricksOpenAI targets (`model="apps/<app-name>"`). They slot into the existing Databricks AI ecosystem rather than replacing any part of it.

### What apx-agent adds

**Typed tools with dependency injection** — Type hints and docstrings generate tool schemas automatically. Parameters typed as `Dependencies.Workspace` or `Dependencies.UserClient` are injected by FastAPI and excluded from the schema — the LLM never sees auth as a parameter, but your function gets a live, per-user authenticated SDK client.

**Automatic OBO token forwarding** — The core problem described above, solved. Every outbound Databricks API call — Genie, Unity Catalog, SQL execution, Vector Search, MCP servers, sub-agent calls — automatically carries the calling user's OAuth token. A single `resolveToken()` handles the full fallback chain: per-request context → explicit headers → `DATABRICKS_TOKEN` env var. Custom tools and third-party connectors get this for free by importing `resolveToken` from the public API.

**Workflow agents** — `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `RouterAgent`, and `HandoffAgent` give you deterministic, developer-defined control flow on top of the LLM. These complement the Supervisor API's probabilistic routing for cases where step order matters.

**Unified tool dispatch** — The LLM loop, the MCP server, and external HTTP callers all invoke tools through the same FastAPI routes (via `ASGITransport` for in-process dispatch). `Dependencies.*` injection — auth, workspace client, OBO tokens — works identically across all three paths without duplicating wiring.

**A2A discovery (`/.well-known/agent.json`)** — Every agent publishes a card with its name, skills, and MCP endpoint. Orchestrating agents fetch this at startup to pull sub-agent capabilities into their own tool list, enabling multi-agent composition without a central schema registry.

**MCP server** — Exposes every registered tool over MCP (SSE and streamable HTTP), wired through the same auth-injecting routes. Connects to Claude Desktop, Cursor, Genie Code, or any Supervisor Agent out of the box.

**Hub** — A lightweight registry that agents self-register with on startup. Provides a browseable index of all running agent apps and powers cross-agent discovery.

## Ecosystem

apx-agent sits alongside a growing set of community and official tools in the Databricks AI space. Here's how they relate:

### Official Databricks projects

| Project | What it does | Relationship |
|---------|-------------|--------------|
| [databrickslabs/mcp](https://github.com/databrickslabs/mcp) | Official Databricks Labs MCP work — managed MCP endpoints for Genie, UC functions | apx-agent exposes your *own* tools over MCP; these endpoints let you *consume* Databricks platform capabilities as MCP tools |
| [databricks-solutions/custom-mcp-databricks-app](https://github.com/databricks-solutions/custom-mcp-databricks-app) | Example: hosting a custom MCP server on a Databricks App with Claude | apx-agent is the full-featured version of this pattern — adds the agent loop, A2A discovery, hub registration, and dev UI on top |
| [databricks-solutions/genierails](https://github.com/databricks-solutions/genierails) | Automates Genie space setup — row filters, column masks, tag policies, guardrails | Orthogonal to apx-agent: use genierails to configure the Genie spaces that `genie_tool()` will call at runtime |

### Community projects

| Project | What it does | Relationship |
|---------|-------------|--------------|
| [alexxx-db/databricks-genie-mcp](https://github.com/alexxx-db/databricks-genie-mcp) | Exposes Genie spaces as MCP tools | apx-agent's `genie_tool()` covers the same ground natively (no separate MCP server needed); this is useful if you want Genie in a non-apx MCP client like Claude Desktop |
| [RafaelCartenet/mcp-databricks-server](https://github.com/RafaelCartenet/mcp-databricks-server) | MCP server for Unity Catalog metadata — table discovery, schema inspection, lineage | Points to the next gap in apx-agent: `catalog_tool()` / `lineage_tool()` / `schema_tool()` factories for agents that need to introspect tables before writing SQL |
| [IanGagnonDB/databricks-agent-mcp-genie](https://github.com/IanGagnonDB/databricks-agent-mcp-genie) | Agent + MCP + Genie integration example | Similar to what apx-agent provides; useful as a reference for Genie conversation patterns |
| [Federix93/genie_space_in_databricks_apps](https://github.com/Federix93/genie_space_in_databricks_apps) | Embeds Genie Conversation API in a Databricks App via Dash | Reference for app-to-Genie wiring patterns; apx-agent's hub uses a similar approach for its chat proxy |

### Where apx-agent fits

The community MCP servers above are **standalone services** — you run them separately and connect clients to them. apx-agent takes a different approach: tool factories (`genie_tool`, `createLakebaseQueryTool`, `createVSQueryTool`) that register directly into your agent's tool loop with the same OBO auth, same schema generation, and same dev UI as your custom tools.

The next logical connectors — `catalog_tool()` for UC table discovery, `lineage_tool()` for upstream/downstream lineage, and `schema_tool()` for column introspection — would close the gap between asking questions (Genie) and knowing the data landscape (UC metadata APIs). The RafaelCartenet MCP server is the clearest prior art for what these should cover.

## Project structure

```
python/          Python package — pyproject.toml, src/, tests/, examples/
typescript/      TypeScript package — package.json, src/, tests/, examples/
hub/             Agent Hub — catalog and chat dashboard (Databricks App)
docs/            Design specs and implementation plans
```

## Configuration

**Python** — `[tool.apx.agent]` in `pyproject.toml`:

```toml
[tool.apx.agent]
name = "my_agent"
description = "What this agent does"
model = "databricks-claude-sonnet-4-6"
instructions = "System prompt for the agent"
max_iterations = 10
sub_agents = ["$OTHER_AGENT_URL"]
```

**TypeScript** — plugin options:

```typescript
agent({
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'System prompt for the agent',
  tools: [myTool],
  subAgents: ['$OTHER_AGENT_URL'],
})
```

Environment variable references (`$VAR` or `${VAR}`) are resolved at startup.

## License

Apache-2.0
