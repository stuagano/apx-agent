# apx-agent

A declarative DSL for building AI agents on Databricks. Author the agent tree once; compile to **Mosaic AI Agent Framework** (Model Serving) by default, or host the same compiled graph in **Databricks Apps** when you need MCP, custom UIs, or stateful workflows. Available in **Python** and **TypeScript**.

## What it is

apx-agent is not a runtime. It's an authoring layer.

You write agents as plain Python or TypeScript:

```python
from apx_agent import Agent, lineage_tool, genie_tool, uc_function_tool

agent = Agent(
    instructions="You investigate missing data.",
    tools=[
        lineage_tool(),
        genie_tool("abc123", description="Answer data questions"),
        uc_function_tool("main.tools.classify_intent"),
    ],
)
```

The framework **compiles** that tree to:

- A LangGraph `StateGraph` (the supported orchestration primitive on Databricks)
- An MLflow `ChatAgent` (the supported Mosaic AI agent contract — recognized by AI Playground, Review App, Agent Evaluation, Supervisor Agent, Genie, MLflow tracing)

From there you have two deployment targets for the same compiled artifact:

| Target | When to use |
|--------|-------------|
| **Model Serving** *(default)* | Stateless agents. Native discovery, identity passthrough, eval, and tracing. Cheaper at low/bursty volume (scale-to-zero). |
| **Databricks Apps** | Stateful workflows, custom UI, MCP server endpoint, long-running background loops (e.g. `EvolutionaryAgent`), or agents that need full FastAPI control. |

apx-agent does not parallel Mosaic AI Agent Framework — it generates artifacts *for* it. Every governance, evaluation, and orchestration feature Databricks ships for `ChatAgent` works automatically because the compile target *is* a `ChatAgent`.

## Why a DSL

Three things you get for free that you'd otherwise hand-wire per agent:

### 1. Declared resources → governed scope

When the compiled `ChatAgent` is logged to MLflow, its resources are declared up front. apx-agent derives this list automatically from the agent's tools, sub-agents, and model:

```python
from apx_agent import log_agent

log_agent(
    agent,
    model="databricks-claude-sonnet-4-6",
    registered_model_name="main.agents.data_triage",
)
# → resources auto-derived:
#   DatabricksServingEndpoint("databricks-claude-sonnet-4-6")  # the LLM
#   DatabricksGenieSpace("abc123")                              # from genie_tool(...)
#   DatabricksFunction("main.tools.classify_intent")            # from uc_function_tool(...)
#   DatabricksServingEndpoint("billing")                        # from sub_agents=[...]
```

Need to declare something apx-agent can't infer (a specific SQL warehouse, vector index, UC table)? Pass `extra_resources=[ResourceSpec("sql_warehouse", "wh-prod"), ...]`.

The platform enforces that the agent can **only** access those resources. Unity Catalog audits every touch. If you grant the agent's service principal broad permissions, the declared-resource list still scopes it down.

### 2. Identity passthrough — the agent runs as the calling user

When the compiled agent is called from a trusted Databricks surface (AI Playground, Genie, Review App, Supervisor Agent, or another registered agent), the calling user's identity is threaded through automatically. The agent's outbound Databricks calls — Unity Catalog reads, Genie queries, SQL execution, vector search — run with **that user's permissions**, scoped to the declared resources.

Practically: *the agent can only do things the user has access to*. You don't write auth code.

### 3. Multi-agent topology — one endpoint per agent

Each compiled agent is its own serving endpoint. A multi-agent system becomes N endpoints registered in UC, with explicit sub-agent declarations and platform-managed token scoping. See [Multi-agent systems](#multi-agent-systems) below.

## Quick start

### Python

```python
from apx_agent import Agent, compile_to_chat_agent, lineage_tool, genie_tool

agent = Agent(
    instructions="You investigate missing data in Databricks tables.",
    tools=[
        lineage_tool(),
        genie_tool("abc123", description="Answer data questions"),
    ],
)

chat_agent = compile_to_chat_agent(agent)
```

Deploy to Model Serving:

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
# → serving endpoint: data-triage
```

`log_agent` walks the agent tree, attaches every tool's declared resources, adds the LLM endpoint, and adds an endpoint reference for each sub-agent. No manual resource list.

Host in Apps instead (same agent, different target):

```python
from apx_agent import create_app
app = create_app(agent)  # uvicorn-compatible FastAPI app
```

### TypeScript

```typescript
import { Agent, compileToChatAgent, lineageTool, genieTool } from 'appkit-agent';

const agent = new Agent({
  instructions: 'You investigate missing data in Databricks tables.',
  tools: [
    lineageTool(),
    genieTool('abc123', { description: 'Answer data questions' }),
  ],
});

const chatAgent = compileToChatAgent(agent);
// log via mlflow / databricks-agents — same flow as Python
```

## The DSL surface

The DSL is the same whether you target Model Serving or Apps. These are the building blocks.

### Typed tools

Define tools as functions with type annotations. Schemas are generated from type hints and docstrings.

```python
def get_table_lineage(table_full_name: str, ws: Dependencies.Workspace) -> dict:
    """Get upstream sources that feed into this table via Unity Catalog lineage."""
    rows = run_sql(ws, f"SELECT ... FROM system.access.table_lineage WHERE target = '{table_full_name}'")
    return {"target": table_full_name, "upstream_sources": rows}
```

`Dependencies.Workspace` is injected at compile time — the LLM never sees it, but your function gets a live, per-request `WorkspaceClient` scoped to the calling user.

```typescript
const getLineage = defineTool({
  name: 'get_table_lineage',
  description: 'Get upstream sources for a table',
  parameters: z.object({ tableName: z.string() }),
  handler: async ({ tableName }) => { /* ... */ },
});
```

### Platform tool factories

Pre-built factories for common Databricks capabilities. One line to register, no schema to write.

| Factory | What it does |
|---------|-------------|
| `genie_tool(space_id)` | Ask a natural-language question to a Genie space |
| `lineage_tool()` | Get upstream/downstream lineage for a UC table |
| `schema_tool()` | Describe columns of a UC table |
| `catalog_tool(catalog, schema)` | List tables in a UC schema |
| `uc_function_tool(function_name)` | Execute a registered UC function |

**`uc_function_tool` is the strongest unlock.** UC functions are already how data teams write and govern business logic — parameter types, documentation, and access controls flow through standard UC governance. Without this, an AI engineer duplicates that work by hand-writing a tool schema; the two definitions then drift apart.

With `uc_function_tool`, the UC function *is* the tool definition. Data teams ship new agent capabilities through their normal workflow — write SQL or Python, register in UC, done — without touching agent code.

```python
agent = Agent(tools=[
    uc_function_tool("main.tools.classify_intent"),
    uc_function_tool("main.tools.score_customer"),
])
```

### Workflow agents

Composable patterns for multi-step orchestration. Each compiles to a deterministic LangGraph topology — the LLM doesn't pick the route, you define it.

| Agent | Purpose |
|-------|---------|
| **SequentialAgent** | Pipeline execution (analyze → plan → execute) |
| **ParallelAgent** | Fan-out / gather (fetch weather + news concurrently) |
| **LoopAgent** | Iterative refinement (draft → review → revise until done) |
| **RouterAgent** | Conditional routing (billing → bill agent, data → triage agent) |
| **HandoffAgent** | Peer handoff mid-conversation (triage → billing) |
| **RemoteAgent** | Cross-endpoint sub-agent call |
| **EvolutionaryAgent** | Population-based search with Pareto selection (Apps-only) |

This complements Mosaic AI Supervisor Agent's probabilistic routing for cases where step order is part of the contract.

#### Durable execution

`SequentialAgent`, `LoopAgent`, and `EvolutionaryAgent` optionally persist each step's output through a pluggable `WorkflowEngine`, so a run can resume after a crash, redeploy, or pause.

| Backend | When to use |
|---------|-------------|
| `InMemoryEngine` | Default — tests, dev, short-lived runs |
| `DeltaEngine` | Production — SQL Statements API against a Delta table; survives restarts |
| `InngestEngine` | Optional adapter — when you already run Inngest as your orchestrator |

Durable workflows generally need Apps hosting — Model Serving is stateless and short-lived per request. See `docs/superpowers/specs/2026-04-19-durable-workflows-design.md`.

## Multi-agent systems

In Model Serving mode, **each agent is its own serving endpoint**. A multi-agent system is N endpoints, each registered in UC, each with its own declared resources.

### Topology

```
                ┌─────────────────────────────────────────┐
                │  AI Playground / Genie / Supervisor /   │
                │  Review App  (caller — owns identity)   │
                └────────────────────┬────────────────────┘
                                     │ user identity threaded
                                     ▼
                   serving-endpoints/orchestrator
                   ┌─────────────────────────────────┐
                   │  main.agents.orchestrator       │
                   │  resources:                     │
                   │   • data-triage   (endpoint)    │
                   │   • billing       (endpoint)    │
                   │   • sql-explainer (endpoint)    │
                   │   • claude-sonnet-4-6 (model)   │
                   └──────┬─────────┬──────────┬─────┘
            ┌─────────────┘         │          └─────────────┐
            ▼                       ▼                        ▼
serving-endpoints/             serving-endpoints/      serving-endpoints/
  data-triage                    billing                 sql-explainer
┌──────────────────────┐     ┌──────────────────────┐  ┌──────────────────────┐
│ main.agents.         │     │ main.agents.         │  │ main.agents.         │
│   data_triage        │     │   billing            │  │   sql_explainer      │
│ resources:           │     │ resources:           │  │ resources:           │
│  • genie_space:abc   │     │  • uc_fn:billing.*   │  │  • warehouse:wh-prod │
│  • warehouse:wh-prod │     │  • warehouse:wh-fin  │  │                      │
└──────────────────────┘     └──────────────────────┘  └──────────────────────┘
```

### How it composes

1. Author each agent as an `apx_agent.Agent`.
2. Compile each to a `ChatAgent` and deploy. Each becomes a Model Serving endpoint backed by a UC-registered model version.
3. In the orchestrator, declare sub-agent endpoints as resources:

   ```python
   orchestrator = Agent(
       instructions="Route the user's question to the right specialist.",
       sub_agents=[
           "endpoints/data-triage",
           "endpoints/billing",
           "endpoints/sql-explainer",
       ],
   )
   ```

4. `compile_to_chat_agent(orchestrator)` emits a `ChatAgent` whose `resources` include `DatabricksServingEndpoint(endpoint_name=...)` entries for each sub-agent.
5. At serving time, the platform mints a token scoped to those endpoints. The orchestrator calls a sub-agent's endpoint over HTTP; the sub-agent receives the call with the *original user's* identity threaded through, scoped to *its own* declared resources, and the chain continues.

### Auth flow

```
User in Playground
     │  identity: alice@example.com
     ▼
┌─ orchestrator endpoint ──────────────────────────┐
│  Platform mints token:                            │
│   • acting as: alice                              │
│   • scoped to: [data-triage, billing, claude-...] │
│  LLM picks "data-triage" sub-agent                │
└────────────────────┬──────────────────────────────┘
                     │ HTTP /serving-endpoints/data-triage/invocations
                     │ Authorization: Bearer <scoped-token>
                     ▼
┌─ data-triage endpoint ────────────────────────────┐
│  Platform mints token:                            │
│   • acting as: alice  (passed through)            │
│   • scoped to: [genie:abc, warehouse:wh-prod]     │
│  Calls Genie + SQL warehouse                      │
│   → Unity Catalog enforces alice's grants         │
└───────────────────────────────────────────────────┘
```

**The agent can only do things alice can do, restricted further to the declared resource list at every hop.** No SP secrets in `app.yaml`, no `CAN_USE` permission grants between apps, no manual token threading. Identity is platform-managed end to end.

### Same code, Apps hosting

The same `orchestrator` can also be hosted in Apps mode (`create_app(orchestrator)`). In that mode, sub-agent URLs point at sibling Databricks Apps, and the auth model changes — see [Apps hosting](#apps-hosting-when-you-need-more) below. You'd choose this path when an agent in the system needs MCP exposure, a custom UI, or persistent state.

### Cost model

| | Model Serving (per endpoint) | Apps (per app) |
|---|---|---|
| **Idle cost** | $0 (scale-to-zero) | Flat per-container |
| **Per request** | Pay-per-token + serving overhead | Included |
| **N agents** | N endpoints, each scale-to-zero | N containers, each flat |
| **Best fit** | Bursty / interactive / sub-agent fan-out | Sustained high-QPS or stateful |

For most multi-agent designs — interactive workflows, agents-as-tools — Model Serving wins on both cost and governance.

## What you get from the compile target

Because the compiled artifact is a `ChatAgent`, the entire Databricks AI surface works without extra wiring:

| Capability | Source |
|------------|--------|
| **Discovery** | Unity Catalog — every registered agent and its declared resources are queryable, auditable, governed |
| **Sub-agent composition** | Native `DatabricksServingEndpoint` resource declarations + Supervisor Agent registration |
| **Identity passthrough** | Automatic from Playground, Genie, Review App, Supervisor — true user-scope across the chain |
| **Tracing** | MLflow auto-instruments every node, tool call, and LLM hop |
| **Evaluation** | Review App + Agent Evaluation work out of the box — they only speak the `ChatAgent` contract |
| **Dev UI** | AI Playground — chat, tool inspection, trace viewer, model swap |
| **Versioning & promotion** | UC Model Registry — staging, production, lineage, rollback |
| **Gateway, rate limits, observability** | Mosaic AI Gateway (where enabled) |

apx-agent doesn't reimplement any of these. The DSL's job is to turn declarative agent code into the artifact that lights all of this up.

## Apps hosting — when you need more

Some workloads don't fit Model Serving's stateless request/response contract. apx-agent's FastAPI host (`create_app(agent)`) runs the same compiled graph and adds:

| Capability | Why it needs Apps |
|------------|-------------------|
| **MCP server** at `/mcp` (streamable HTTP transport) | Model Serving exposes only `/invocations`; MCP needs an arbitrary HTTP route. Connects to Claude Desktop, Cursor, Genie Code, Supervisor Agent. |
| **Custom UI** | Apps can serve HTML/React from the same process. |
| **Long-running state** | `EvolutionaryAgent`'s population loop, websockets, background workers, in-memory caches. |
| **A2A discovery card** at `/.well-known/agent.json` | UC + Mosaic AI registration is the Model Serving equivalent; the card is useful when integrating outside the Databricks surface. |
| **Hub auto-registration** | Lightweight registry for cross-app discovery in Apps deployments. |
| **Dev UI** (`/_apx/agent`, `/_apx/tools`, `/_apx/probe`) | Mirrors Playground for local dev when you're not deployed yet. |

### Auth in Apps mode

When agents run as Apps, inbound user identity arrives via `X-Forwarded-Access-Token` (Databricks Apps SSO gateway). apx-agent captures it at the middleware boundary, propagates it through the async context, and resolves it at every outbound call:

| Priority | Source | When |
|----------|--------|------|
| 1 | Per-request OBO context | Interactive — user hits the app, their token flows through |
| 2 | Explicit headers | Caller passes auth directly (testing, manual invocation) |
| 3 | `DATABRICKS_TOKEN` env var | Local dev with a static PAT |
| 4 | M2M OAuth (`DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET`) | Background jobs, evolutionary loops — no user present |

### App-to-app calls

When an orchestrator App calls a sub-agent App, the user-identity story is weaker than Model Serving's — the call goes from the orchestrator's service principal to the sub-agent's service principal, with `CAN_USE` permissions granted in between. This works but loses true user-scope at the first hop. If user-scoped multi-agent matters, prefer Model Serving deployment.

Routes mounted under `/api/` accept bearer tokens; all other routes trigger interactive SSO. apx-agent mounts every endpoint at both its natural path and an `/api/` mirror.

```bash
# 1. Mint an OAuth secret for each app's SP
databricks api post /api/2.0/accounts/servicePrincipals/<SP_ID>/credentials/secrets \
  --profile <profile> --json '{}'

# 2. Configure in each app.yaml
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

## How it fits

Databricks ships native paths for building agents. apx-agent slots into them:

| Path | Routing | Where apx-agent fits |
|------|---------|----------------------|
| **Mosaic AI Agent Framework** (`ChatAgent`, MLflow, Model Serving) | LLM-driven, agent-internal | apx-agent's **default compile target**. The DSL generates ChatAgents. |
| **Mosaic AI Supervisor Agent** | LLM picks sub-agent | apx-agent-compiled agents register as Supervisor sub-agents natively (they're endpoints). Use Supervisor when you want managed routing; use apx-agent's `RouterAgent` / `HandoffAgent` when routing is part of your contract. |
| **LangGraph directly** | Developer-defined graph | apx-agent compiles to LangGraph; if you want raw `StateGraph` control, drop down. |

apx-agent extends — not replaces — the native path. The compiled artifact is whatever Databricks ships next on the `ChatAgent` contract; the DSL stays the same.

## Ecosystem

Other tools in the Databricks AI space and how they relate:

### Official Databricks projects

| Project | Relationship |
|---------|--------------|
| [databrickslabs/mcp](https://github.com/databrickslabs/mcp) | Managed MCP endpoints for Genie, UC functions, vector search. apx-agent exposes your *own* tools over MCP (Apps mode); these expose Databricks platform capabilities as MCP. |
| [databricks-solutions/custom-mcp-databricks-app](https://github.com/databricks-solutions/custom-mcp-databricks-app) | Reference for hosting a custom MCP server on Databricks Apps. apx-agent is the full-featured pattern — DSL, agent loop, A2A discovery, hub registration, dev UI. |
| [databricks-solutions/genierails](https://github.com/databricks-solutions/genierails) | Configures Genie spaces (row filters, masks, guardrails). Orthogonal: use genierails to configure the spaces that `genie_tool()` calls at runtime. |

### Community projects

| Project | Relationship |
|---------|--------------|
| [alexxx-db/databricks-genie-mcp](https://github.com/alexxx-db/databricks-genie-mcp) | Genie spaces over MCP. apx-agent's `genie_tool()` covers the same ground natively; the MCP version is useful in non-apx clients. |
| [RafaelCartenet/mcp-databricks-server](https://github.com/RafaelCartenet/mcp-databricks-server) | UC metadata over MCP. Prior art for apx-agent's `catalog_tool` / `lineage_tool` / `schema_tool`. |
| [IanGagnonDB/databricks-agent-mcp-genie](https://github.com/IanGagnonDB/databricks-agent-mcp-genie) | Reference for Genie conversation patterns. |
| [Federix93/genie_space_in_databricks_apps](https://github.com/Federix93/genie_space_in_databricks_apps) | Reference for Genie + Apps wiring. |

## Project structure

```
python/          Python package — DSL, compile path, ChatAgent wrapper, FastAPI host
typescript/      TypeScript package — same surface, same compile targets
hub/             Agent Hub — catalog and chat dashboard (Apps-mode discovery)
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
sub_agents = ["endpoints/sql-explainer"]  # Model Serving target
# sub_agents = ["$SQL_EXPLAINER_URL"]      # Apps target
```

**TypeScript** — `createApp` plugin options:

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
