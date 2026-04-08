# apx-agent

Agent toolkit for Databricks Apps — typed tools, MCP exposure, A2A discovery, registry auto-registration, and dev UI. Available as a **Python package** (production, deployed today) and a **TypeScript AppKit plugin scaffold** (future direction).

## Where apx-agent fits

Databricks offers multiple approaches to multi-agent orchestration. apx-agent targets the middle ground: more structured than "write your own LangGraph" but more controllable than "hope the LLM picks the right tool."

| Need | Solution | Routing | Auth | Custom logic |
|------|----------|---------|------|-------------|
| Simple multi-tool, LLM-routed | **Supervisor API** (native) | Probabilistic — LLM picks tools based on descriptions | OBO automatic | Instructions only |
| Deterministic routing, conditional logic, workflows | **apx-agent** | Deterministic — developer controls flow | OBO via headers + DatabricksOpenAI | Full Python/TS control |
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
        {"type": "agent_endpoint", "agent_endpoint": {"name": "...", "endpoint_name": "...", "description": "..."}},
        {"type": "external_mcp_server", "external_mcp_server": {"connection_name": "...", "description": "..."}},
    ],
    stream=True,
)
```

**Strengths:** Fully OBO (user token flows through entire chain — no SP credentials needed), server-managed tool execution, model swappable per request, AI Gateway integration with tracing.

**Limitations:** 100% probabilistic routing (LLM decides which tool to call based on descriptions), no conditional logic, no deterministic workflows, no custom business logic, cannot mix server-side and client-side tools, max 20 tools. "Multi-agent" in Supervisor means agents-as-tools executed server-side — not peer-to-peer agent communication or A2A protocol.

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

---

## Migration plan: aligning with DatabricksOpenAI + OpenAI Agents SDK

apx-agent currently uses a custom LLM loop. The plan is to migrate the runtime to official Databricks primitives while keeping the unique DX/ops layer.

### Gap matrix

| Component | Current apx-agent | Official Databricks | Action |
|-----------|-------------------|---------------------|--------|
| **LLM loop** | Custom httpx POST to FMAPI, Chat Completions format | `Runner.run()` from OpenAI Agents SDK + `DatabricksOpenAI` | **REPLACE** |
| **Serving** | Custom FastAPI `create_app()` | MLflow `AgentServer` with `@invoke`/`@stream` | **WRAP** (keep create_app, delegate /invocations) |
| **Tool definition** | Typed functions + FastAPI DI + auto routes | `@function_tool` (no DI) | **WRAP** (keep apx-agent's, add adapter for Runner) |
| **Sub-agent calls** | Raw httpx to `/invocations` | `DatabricksOpenAI.responses.create(model="apps/<name>")` | **REPLACE** (gets automatic OBO) |
| **MCP serving** | Full SSE + streamable HTTP server | Not built-in (SDK only consumes MCP) | **KEEP** |
| **Dependencies / OBO** | `Dependencies.Workspace`, `.Sql`, `.Headers` | Nothing equivalent | **KEEP** |
| **A2A discovery** | `/.well-known/agent.json` | Nothing equivalent | **KEEP** |
| **Registry** | Auto-register on startup | Nothing equivalent | **KEEP** |
| **Dev UI** | Chat + tool inspector + probe | Basic MLflow chat proxy | **KEEP** |
| **Workflow agents** | Sequential, Parallel, Loop, Router, Handoff | Only handoffs in SDK | **KEEP** (swap internal loop call) |
| **Streaming** | Fake (run-to-completion, chunk) | Real token streaming via `Runner.run_streamed()` | **REPLACE** |
| **Tracing** | Manual MLflow spans | Automatic via DatabricksOpenAI | **REPLACE** |

### Priority order

| # | Change | Risk | Value |
|---|--------|------|-------|
| P0 | Add `databricks-openai` + `openai-agents` as deps | Low | Prerequisite |
| P1 | Create `_runner.py` adapter (both paths coexist) | Low | High — enables incremental migration |
| P2 | Feature-flag `USE_RUNNER=true`, test on explain-my-bill agent | Medium | High — core swap |
| P3 | Real streaming via `Runner.run_streamed()` | Low | Medium |
| P4 | Delete manual MLflow spans | Low | Cleanup |
| P5 | Sub-agent dispatch via `model="apps/<name>"` | Medium | Medium — automatic OBO |
| P7 | AgentServer co-hosting option | Low | Wait for API stability |

**11 of 17 source files stay untouched.** ~280 lines deleted from the custom loop, ~80 lines added in the adapter. No breaking changes to the 6 deployed agents.

---

## Vision: AppKit Supervisor SDK

Looking forward, the agent orchestration layer should be a TypeScript-native AppKit plugin built on top of the Supervisor API. This is a design sketch — not an existing package — showing what `@databricks/appkit-supervisor` could look like.

### Define a Supervisor on AppKit

```typescript
// supervisor.ts
import {
  defineSupervisor,
  genieAgent,
  knowledgeAssistantAgent,
  ucFunctionTool,
  mcpTool,
  when,
  loop,
} from '@databricks/appkit-supervisor';

const portfolioGenie = genieAgent({
  id: 'genie-space:portfolio-analytics',
  displayName: 'Portfolio Analytics',
});

const supportKA = knowledgeAssistantAgent({
  id: 'ka:endpoint:customer-support',
  displayName: 'Support Docs',
});

const sqlRunner = ucFunctionTool({
  fqName: 'finance.analytics.run_sql',
});

const jiraMcp = mcpTool({
  name: 'jira',
  connection: 'uc_connection:jira_prod',
});

export const financialSupervisor = defineSupervisor({
  name: 'financial_supervisor',
  description: 'Routes questions across portfolio metrics, support docs, and JIRA.',
  instructions: [
    'You are a financial copilot for PMs and support engineers.',
    'Prefer portfolioGenie for metrics / positions / P&L.',
    'Prefer supportKA for policy and process questions.',
    'Use sqlRunner only when you need custom SQL not covered by Genie.',
    'Use jiraMcp only when explicitly asked to create/update tickets.',
  ],
  // Deterministic routing layer on top of Supervisor's probabilistic orchestration
  routes: [
    when.intent('portfolio_question').routeTo(portfolioGenie),
    when.intent('support_question').routeTo(supportKA),
    when.intent('incident_ticket').routeTo(jiraMcp),
  ],
  loop: loop({
    maxIterations: 6,
    stopWhen: 'answer_is_confident_or_user_says_stop',
  }),
  model: {
    default: 'databricks-gpt-4.1-mini',
  },
});
```

### Sync to Supervisor Agent via REST

```typescript
// deploy.ts — run from CI or CLI
import { WorkspaceClient } from '@databricks/sdk';

export async function syncSupervisor() {
  const w = new WorkspaceClient();
  // compile() → payload shaped like /api/2.1/supervisor-agent create request
  const payload = financialSupervisor.compile();
  await w.apiClient.post('/api/2.1/supervisor-agent', payload);
}
```

### React chat UI

```tsx
// client/ChatView.tsx
import React, { useState } from 'react';
import { useSupervisorChat } from '@databricks/appkit-supervisor/react';
import { financialSupervisor } from '../supervisor';

export function ChatView() {
  const [input, setInput] = useState('');
  const { messages, send, loading } = useSupervisorChat({
    supervisor: financialSupervisor,
    endpoint: 'https://<workspace>/serving-endpoints/sa-financial-supervisor',
    sessionId: 'user-123-session-abc',
  });

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto space-y-2 p-4">
        {messages.map(m => (
          <div key={m.id} className={m.role === 'user' ? 'text-right' : 'text-left'}>
            <div className="inline-block px-3 py-2 rounded bg-slate-100">
              {m.content}
            </div>
          </div>
        ))}
      </div>
      <form
        className="p-4 flex gap-2 border-t"
        onSubmit={e => {
          e.preventDefault();
          if (!input.trim()) return;
          send(input);
          setInput('');
        }}
      >
        <input
          className="flex-1 border px-3 py-2 rounded"
          value={input}
          onChange={e => setInput(e.target.value)}
          placeholder="Ask about portfolio or support..."
        />
        <button
          type="submit"
          disabled={loading}
          className="px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
        >
          {loading ? 'Thinking...' : 'Send'}
        </button>
      </form>
    </div>
  );
}
```

### Key ideas

- **`defineSupervisor`** — TypeScript DSL that compiles to Supervisor Agent config (name, description, instructions, sub-agents, tools, routing hints, loop policy)
- **Typed agent/tool helpers** (`genieAgent`, `knowledgeAssistantAgent`, `ucFunctionTool`, `mcpTool`) keep sub-agent wiring typed and discoverable
- **`routes` + `loop`** — optional deterministic layer on top of Supervisor's probabilistic orchestration, addressing the [gap customers are asking for](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/workflow) (EY, Crypto.com need guaranteed routing, not LLM guesses)
- **`useSupervisorChat` hook** — thin AppKit/React wrapper over the Supervisor Agent endpoint with pluggable chat history (Lakebase, session store)
- **`compile()` + `syncSupervisor()`** — infrastructure-as-code pattern: define in TypeScript, deploy via CI, no manual UI configuration

### How this relates to apx-agent today

The Python `apx-agent` package is the working prototype of these patterns. The TypeScript AppKit plugins in `ts/` are the scaffold for the future. The migration path:

1. **Today:** Python apx-agent ships agents that work in AI Playground, Genie Code, Claude Desktop, and as Supervisor sub-agents
2. **Next:** Migrate the Python LLM loop to `DatabricksOpenAI` + `Runner.run()` (P0-P5 above)
3. **Future:** When Supervisor SDK ships in AppKit, the TypeScript plugins become the primary development path, and the AppKit Supervisor SDK vision above becomes real

---

## Python package

### Quick start

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

### Features

#### Typed tool registration

Define tools as plain Python functions. Type hints become the input schema, the docstring becomes the description. `Dependencies.*` parameters are injected by FastAPI and excluded from the schema.

```python
def get_table_lineage(table_full_name: str, ws: Dependencies.Workspace) -> dict:
    """Get upstream sources that feed into this table via Unity Catalog lineage."""
    rows = run_sql(ws, f"SELECT ... FROM system.access.table_lineage WHERE target = '{table_full_name}'")
    return {"target": table_full_name, "upstream_sources": rows}
```

#### A2A discovery

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

#### Registry auto-registration

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

#### Sub-agent composition

Agents can call other agents deployed as Databricks Apps:

```python
agent = Agent(
    tools=[get_table_lineage, find_jobs_for_table],
    sub_agents=["$DATA_INSPECTOR_URL"],
    instructions="Use the data_inspector for SQL queries and Delta forensics.",
)
```

#### MCP server

Every agent exposes MCP at `/mcp/sse` (SSE transport) and `/mcp` (streamable HTTP). Connect from Claude Desktop, Cursor, Genie Code, or Supervisor Agent.

#### Dev UI

- `/_apx/agent` — chat interface for testing
- `/_apx/tools` — tool inspector with live invocation forms
- `/_apx/probe?url=<url>` — outbound connectivity tester

### Configuration

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

---

## TypeScript AppKit plugins (scaffold)

Four composable plugins for building AI agents on Databricks AppKit:

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

| Plugin | What it does |
|--------|-------------|
| `agent` | Agent loop + typed tool registration (Zod schemas), `/invocations` endpoint |
| `discovery` | A2A card at `/.well-known/agent.json`, registry auto-registration |
| `mcp` | MCP server for Supervisor Agent, Claude Desktop, Cursor |
| `devUI` | Chat UI + tool inspector + connectivity probe (dev-only) |

See `ts/` for the full scaffold.
