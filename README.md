# apx-agent

A declarative framework for building governed AI agents on Databricks. Available in **Python** and **TypeScript**.

## What's here

Three building blocks. Everything else is built on top.

### 1. Governed primitives

Unity Catalog functions, Genie spaces, vector search indices, SQL warehouses — turn each into a tool with one line. Every tool factory declares itself as a Mosaic AI resource so that at deploy time, the platform mints a scoped token and Unity Catalog enforces user-scoped grants on every call.

```python
from apx_agent import Agent, uc_function_tool, genie_tool

agent = Agent(tools=[
    uc_function_tool("main.tools.classify_intent"),
    uc_function_tool("main.tools.score_customer"),
    genie_tool("abc123", description="Answer sales data questions"),
])
```

### 2. Identity passthrough

The calling user's OAuth token flows through every tool, every sub-agent call, and every outbound Databricks API call. The agent runs as the user — not as a service principal. UC enforces *their* grants on *their* data. No auth code at the tool level; the framework handles capture, propagation, and resolution.

### 3. Workflow patterns

Composable agent patterns for multi-step orchestration. The LLM doesn't pick the route when the route is part of the contract.

| Agent | Purpose |
|-------|---------|
| **SequentialAgent** | Pipeline execution (analyze → plan → execute) |
| **ParallelAgent** | Fan-out / gather (fetch weather + news concurrently) |
| **LoopAgent** | Iterative refinement (draft → review → revise until done) |
| **RouterAgent** | Conditional routing (billing → bill agent, data → triage agent) |
| **HandoffAgent** | Peer handoff mid-conversation (triage → billing) |
| **RemoteAgent** | Cross-endpoint sub-agent call |

## Quick start

### Python

```python
from apx_agent import Agent, genie_tool, lineage_tool, uc_function_tool

agent = Agent(
    instructions="You investigate missing data in Databricks tables.",
    tools=[
        lineage_tool(),
        genie_tool("abc123", description="Answer data questions"),
        uc_function_tool("main.tools.classify_intent"),
    ],
)
```

Deploy as a Mosaic AI agent (Model Serving):

```python
import mlflow
from databricks import agents
from apx_agent import log_agent

with mlflow.start_run():
    info = log_agent(
        agent,
        model="databricks-claude-sonnet-4-6",
        registered_model_name="main.agents.data_triage",
    )

agents.deploy("main.agents.data_triage", model_version=info.registered_model_version)
```

`log_agent` walks the agent tree, collects every declared resource (UC functions, Genie spaces, sub-agent endpoints, the LLM endpoint), and hands MLflow the full list. No manual `resources=[...]` to maintain.

Host as a Databricks App instead (same agent, different runtime):

```python
from apx_agent import create_app
app = create_app(agent)  # uvicorn-compatible FastAPI app
```

```bash
cd python
uv sync
uvicorn my_app:app --reload
```

### TypeScript

```typescript
import { createApp, server } from '@databricks/appkit';
import { createAgentPlugin, lineageTool, genieTool, ucFunctionTool } from 'appkit-agent';

createApp({
  plugins: [
    server(),
    createAgentPlugin({
      model: 'databricks-claude-sonnet-4-6',
      instructions: 'You investigate missing data.',
      tools: [
        lineageTool(),
        genieTool('abc123', { description: 'Answer data questions' }),
        ucFunctionTool('main.tools.classify_intent'),
      ],
    }),
  ],
});
```

```bash
cd typescript
npm install
npm run dev
```

## Governed primitives

### UC functions are the unlock

UC functions are already how data teams write and govern business logic. They define parameter types, write documentation, and apply access controls through standard UC governance. Without a UC function tool, an AI engineer duplicates that work by hand-writing a tool schema and a call implementation that mirrors what the data team already registered. The two definitions then drift apart.

With `uc_function_tool`, the UC function *is* the tool definition. The data team owns the logic; the AI engineer registers it in one line. Governance, access control, and documentation flow through UC the same way they do for any other data asset. Data teams ship new agent capabilities through their normal workflow — write SQL or Python, register in UC, done — without touching agent code.

```sql
-- Data team writes & registers the function in UC
CREATE OR REPLACE FUNCTION main.tools.classify_intent(query STRING)
RETURNS STRING
COMMENT 'Classify a customer query as: billing, technical, account, other.'
LANGUAGE PYTHON
AS $$
  # ... implementation
$$;

GRANT EXECUTE ON FUNCTION main.tools.classify_intent TO `agent_consumers`;
```

```python
# AI engineer composes the agent
from apx_agent import Agent, uc_function_tool

agent = Agent(tools=[
    uc_function_tool("main.tools.classify_intent"),
])
```

When the agent runs, the user's grants on `main.tools.classify_intent` apply. If they can't execute it directly, the agent can't either. The function's `COMMENT` becomes the tool description; parameter types become the tool schema. One source of truth.

### Platform tool factories

| Factory | What it does |
|---------|-------------|
| `uc_function_tool(name)` | Execute a registered UC function. Schema auto-derived from UC. |
| `genie_tool(space_id)` | Ask a natural-language question to a Genie space |
| `lineage_tool()` | Get upstream/downstream lineage for a UC table |
| `schema_tool()` | Describe columns of a UC table |
| `catalog_tool(catalog, schema)` | List tables in a UC schema |

Each factory attaches its resource declaration to the returned tool. `log_agent` collects them automatically.

### Declared resources

When the agent is logged to MLflow for Model Serving, its resources are declared up front:

```python
log_agent(
    agent,
    model="databricks-claude-sonnet-4-6",
    registered_model_name="main.agents.data_triage",
)
# resources auto-derived from the agent tree:
#   DatabricksServingEndpoint("databricks-claude-sonnet-4-6")  # the LLM
#   DatabricksGenieSpace("abc123")                              # from genie_tool(...)
#   DatabricksFunction("main.tools.classify_intent")            # from uc_function_tool(...)
#   DatabricksServingEndpoint("billing")                        # from sub_agents=[...]
```

The platform enforces that the agent can **only** access those resources. Need to declare something the framework can't infer (a specific SQL warehouse, vector index, UC table)? Pass `extra_resources=[ResourceSpec("sql_warehouse", "wh-prod"), ...]`.

### Identity passthrough

In Databricks Apps, the user's OAuth token arrives as `X-Forwarded-Access-Token`. The framework captures it at the middleware boundary, propagates it through the async context, and resolves it at every outbound call.

```python
def get_table_lineage(table_full_name: str, ws: Dependencies.Workspace) -> dict:
    """Get upstream sources that feed into this table."""
    # ws is a per-request WorkspaceClient scoped to the calling user.
    # All Databricks API calls through ws run with that user's grants.
    rows = run_sql(ws, f"SELECT ... WHERE target = '{table_full_name}'")
    return {"target": table_full_name, "upstream_sources": rows}
```

Resolution order:

| Priority | Source | When it's used |
|----------|--------|---------------|
| 1 | Per-request OBO context | Interactive — user hits the app, their token flows through |
| 2 | Explicit headers | Caller passes auth directly (testing, manual invocation) |
| 3 | `DATABRICKS_TOKEN` env var | Local dev with a static PAT |
| 4 | M2M OAuth (`DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET`) | Background jobs, workflows — no user present |

When deployed to Model Serving and called from a trusted Databricks surface (AI Playground, Genie, Review App, Supervisor sub-agent), the calling user's identity threads automatically through the agent and its sub-agents — scoped to the declared resources at every hop.

## Workflow patterns

### Sequential — multi-step pipelines

Each step receives the previous step's output as context. Use when step order is part of the contract.

```python
from apx_agent import Agent, SequentialAgent, lineage_tool, schema_tool

pipeline = SequentialAgent([
    Agent(instructions="Identify which tables the user is asking about.",
          tools=[lineage_tool(), schema_tool()]),
    Agent(instructions="Plan a multi-step investigation."),
    Agent(instructions="Execute the plan and report findings."),
])
```

### Parallel — fan-out / gather

Run sub-agents concurrently, merge results. Use for independent lookups that don't depend on each other.

```python
from apx_agent import ParallelAgent

merged = ParallelAgent([
    Agent(instructions="Get weather", tools=[weather_tool]),
    Agent(instructions="Get news", tools=[news_tool]),
])
```

### Loop — iterative refinement

Repeat a sub-agent until it calls `finish_loop()` or hits `max_iterations`. Use for draft → review → revise patterns.

```python
from apx_agent import LoopAgent

drafter = Agent(instructions="Draft a response. Call finish_loop when satisfied.")
refiner = LoopAgent(drafter, max_iterations=5)
```

### Router — conditional routing

LLM picks one branch based on the user's input. Use when the route is data-dependent but the branch agents are fixed.

```python
from apx_agent import RouterAgent

router = RouterAgent({
    "billing": billing_agent,
    "technical": tech_agent,
    "data": data_agent,
})
```

### Handoff — peer handoff mid-conversation

An agent transfers the conversation to a peer agent. The new agent inherits the conversation state.

```python
from apx_agent import HandoffAgent

triage = HandoffAgent(
    instructions="Triage the user's question, then hand off to the right specialist.",
    targets={
        "billing_agent": billing_agent,
        "technical_agent": tech_agent,
    },
)
```

### Sub-agents — cross-endpoint composition

When sub-agents are deployed as separate Model Serving endpoints or Databricks Apps:

```python
# Model Serving target — sub-agents become DatabricksServingEndpoint resources
agent = Agent(
    instructions="Route the user's question to the right specialist.",
    sub_agents=[
        "endpoints/data-triage",
        "endpoints/billing",
        "endpoints/sql-explainer",
    ],
)
```

```python
# Databricks Apps target — sub-agents are sibling Apps
agent = Agent(
    instructions="Route the user's question to the right specialist.",
    sub_agents=[
        "$DATA_TRIAGE_URL",  # $VAR expanded at startup
        "$BILLING_URL",
    ],
)
```

When deployed to Model Serving, sub-agent endpoints are auto-declared as resources — calls to them flow through Mosaic AI's identity passthrough. When hosted in Apps, sub-agent calls go through the app-to-app auth path (see below).

### Durable execution

`SequentialAgent`, `LoopAgent`, and `EvolutionaryAgent` can persist each step's output through a pluggable `WorkflowEngine` — a run can resume after a crash, redeploy, or pause.

| Backend | When to use |
|---------|-------------|
| `InMemoryEngine` | Default — tests, dev, short interactive runs |
| `DeltaEngine` | Production — SQL Statements API against a Delta table; survives restarts |
| `InngestEngine` | Optional adapter — when you already run Inngest as your orchestrator |

Durable workflows generally need Apps hosting — Model Serving is stateless and short-lived per request. See `docs/superpowers/specs/2026-04-19-durable-workflows-design.md`.

## Typed tools — for custom code

Define tools as functions with type annotations. The framework generates input schemas and descriptions from type hints and docstrings.

**Python** — type hints + docstrings, with `Dependencies.*` parameters injected by FastAPI:

```python
def get_jobs_for_table(table_full_name: str, ws: Dependencies.Workspace) -> list[dict]:
    """Find Databricks Jobs that write to a Unity Catalog table."""
    rows = run_sql(ws, f"SELECT job_id, name FROM system.lakeflow.jobs WHERE ...")
    return rows
```

**TypeScript** — Zod schemas + handler functions:

```typescript
const getJobs = defineTool({
  name: 'get_jobs_for_table',
  description: 'Find Databricks Jobs that write to a UC table',
  parameters: z.object({ tableName: z.string() }),
  handler: async ({ tableName, ws }) => { /* ... */ },
});
```

Custom tools can declare their own resources:

```python
from apx_agent import ResourceSpec, attach_resources

def query_orders(question: str, ws: Dependencies.Workspace) -> str:
    """Query the orders Delta table."""
    return run_sql(ws, f"SELECT ... FROM main.sales.orders WHERE ...")

attach_resources(query_orders, [ResourceSpec("uc_table", "main.sales.orders")])
```

`log_agent` picks these up the same way it picks up the platform factories.

## Deployment

### Model Serving (Mosaic AI)

The default path for stateless, multi-surface agents. `log_agent` produces an MLflow `ChatAgent` with declared resources; `databricks.agents.deploy` creates the serving endpoint. The deployed agent is recognized natively by AI Playground, Review App, Agent Evaluation, MLflow tracing, and the Supervisor Agent as a sub-agent.

- **Pay-per-request** — scale-to-zero, no idle cost
- **Identity passthrough** automatic from Playground / Genie / Supervisor
- **Stateless** — request/response only; no persistent state between calls

### Databricks Apps

When you need state, custom UI, MCP server endpoint, or long-running workflows. `create_app(agent)` wraps the same agent as a FastAPI service with the full apx-agent host: OBO middleware, `/responses` endpoint, `/mcp` MCP server, `/.well-known/agent.json` discovery card, hub auto-registration, dev UI at `/_apx/*`.

- **Flat-rate compute** — cheaper at sustained traffic, more expensive idle
- **Stateful** — in-memory caches, background loops, websockets, custom UI all work
- **OBO automatic** for browser/SSO traffic via `X-Forwarded-Access-Token`

## MCP server

Every Apps-hosted agent exposes MCP at `/mcp` (streamable HTTP transport). Connect from Claude Desktop, Cursor, Genie Code, or any MCP-aware agent.

```bash
# Local agent → MCP server at http://localhost:8000/mcp
# Deployed agent → MCP server at https://<app>.databricksapps.com/mcp
```

Model Serving agents don't expose `/mcp` — Model Serving has a fixed `/invocations` endpoint with the `ChatAgent` contract. Use Apps hosting when MCP exposure matters.

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

## Hub

A lightweight registry for Apps-deployed agents. Agents self-register on startup. Provides a browseable index and powers cross-agent discovery in Apps deployments. UC + Mosaic AI's agent registry serves the same role for Model Serving deployments.

```toml
# python/pyproject.toml
[tool.apx.agent]
name = "data_triage_agent"
description = "Investigate missing data"
model = "databricks-claude-sonnet-4-6"
registry = "$AGENT_HUB_URL"
```

## Dev UI

Apps-hosted agents include built-in development tooling at:
- `/_apx/agent` — chat interface for testing
- `/_apx/tools` — tool inspector with live invocation
- `/_apx/probe?url=<url>` — outbound connectivity tester

Model Serving deployments use AI Playground as the equivalent surface.

## Ecosystem

How apx-agent relates to other tools in the Databricks AI space:

### Official Databricks projects

| Project | Relationship |
|---------|--------------|
| [databrickslabs/mcp](https://github.com/databrickslabs/mcp) | Managed MCP endpoints for Genie, UC functions, vector search. apx-agent exposes your *own* tools over MCP (Apps mode); these expose Databricks platform capabilities as MCP. |
| [databricks-solutions/custom-mcp-databricks-app](https://github.com/databricks-solutions/custom-mcp-databricks-app) | Reference for hosting a custom MCP server on Databricks Apps. apx-agent is the full-featured pattern — agent loop, A2A discovery, hub registration, dev UI. |
| [databricks-solutions/genierails](https://github.com/databricks-solutions/genierails) | Configures Genie spaces (row filters, masks, guardrails). Orthogonal: use genierails to configure the spaces that `genie_tool()` calls at runtime. |

### Community projects

| Project | Relationship |
|---------|--------------|
| [alexxx-db/databricks-genie-mcp](https://github.com/alexxx-db/databricks-genie-mcp) | Genie spaces over MCP. apx-agent's `genie_tool()` covers the same ground natively; the MCP version is useful in non-apx clients. |
| [RafaelCartenet/mcp-databricks-server](https://github.com/RafaelCartenet/mcp-databricks-server) | UC metadata over MCP. Prior art for apx-agent's `catalog_tool` / `lineage_tool` / `schema_tool`. |

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
name = "data_triage"
description = "Investigate missing data"
model = "databricks-claude-sonnet-4-6"
instructions = "System prompt for the agent"
max_iterations = 10
sub_agents = ["endpoints/sql-explainer"]   # Model Serving target
# sub_agents = ["$SQL_EXPLAINER_URL"]      # Apps target
```

**TypeScript** — plugin options:

```typescript
createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'System prompt for the agent',
  tools: [myTool],
  subAgents: ['endpoints/sql-explainer'],
})
```

Environment variable references (`$VAR` or `${VAR}`) are resolved at startup.

## License

Apache-2.0
