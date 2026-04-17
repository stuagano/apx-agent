# apx-agent

A standard set of tools for building AI agents on Databricks Apps. Available in **Python** and **TypeScript**.

Both implementations share the same capabilities: typed tool registration, MCP server, A2A discovery, sub-agent composition, registry auto-registration, and a dev UI.

## Quick start

### Python

```python
from apx_agent import Agent, Dependencies, create_app

def get_billing(customer_id: str, ws: Dependencies.Workspace) -> dict:
    """Get billing history for a customer."""
    ...

agent = Agent(tools=[get_billing])
app = create_app(agent)
```

```bash
cd python
uv sync
uvicorn my_app:app --reload
```

### TypeScript

```typescript
import { createApp, server, genie } from '@databricks/appkit';
import { agent, discovery, mcp, devUI, defineTool } from 'appkit-agent';
import { z } from 'zod';

const getLineage = defineTool({
  name: 'get_table_lineage',
  description: 'Get upstream sources for a table',
  parameters: z.object({ tableName: z.string() }),
  handler: async ({ tableName }) => { /* query UC lineage */ },
});

createApp({
  plugins: [
    server(),
    genie(),
    agent({
      model: 'databricks-claude-sonnet-4-6',
      instructions: 'You investigate missing data.',
      tools: [getLineage],
    }),
    discovery({ registry: '$AGENT_HUB_URL' }),
    mcp(),
    devUI(),
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

**OBO token forwarding** — Databricks Apps authenticate users via OAuth On-Behalf-Of tokens. apx-agent propagates these automatically across app-to-app calls: it first routes through the Supervisor gateway (`model="apps/<name>"`) for OBO, then falls back to forwarding `X-Forwarded-Access-Token` directly. Your tools always run as the calling user, not the app's service principal.

**Workflow agents** — `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `RouterAgent`, and `HandoffAgent` give you deterministic, developer-defined control flow on top of the LLM. These complement the Supervisor API's probabilistic routing for cases where step order matters.

**Unified tool dispatch** — The LLM loop, the MCP server, and external HTTP callers all invoke tools through the same FastAPI routes (via `ASGITransport` for in-process dispatch). `Dependencies.*` injection — auth, workspace client, OBO tokens — works identically across all three paths without duplicating wiring.

**A2A discovery (`/.well-known/agent.json`)** — Every agent publishes a card with its name, skills, and MCP endpoint. Orchestrating agents fetch this at startup to pull sub-agent capabilities into their own tool list, enabling multi-agent composition without a central schema registry.

**MCP server** — Exposes every registered tool over MCP (SSE and streamable HTTP), wired through the same auth-injecting routes. Connects to Claude Desktop, Cursor, Genie Code, or any Supervisor Agent out of the box.

**Hub** — A lightweight registry that agents self-register with on startup. Provides a browseable index of all running agent apps and powers cross-agent discovery.

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
