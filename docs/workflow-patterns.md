# Workflow patterns

Composable agent patterns for multi-step orchestration. The LLM doesn't pick the route when the route is part of the contract.

| Agent | Purpose |
|-------|---------|
| `SequentialAgent` | Pipeline execution (analyze → plan → execute) |
| `ParallelAgent` | Fan-out / gather (fetch weather + news concurrently) |
| `LoopAgent` | Iterative refinement (draft → review → revise until done) |
| `RouterAgent` | Conditional routing (billing → bill agent, data → triage agent) |
| `HandoffAgent` | Peer handoff mid-conversation (triage → billing) |
| `RemoteAgent` | Cross-endpoint sub-agent call |
| `agent_tool` | LLM-driven delegation — wrap any agent as a callable tool |

## Sequential — multi-step pipelines

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

## Parallel — fan-out / gather

Run sub-agents concurrently, merge results. Use for independent lookups that don't depend on each other.

```python
from apx_agent import ParallelAgent

merged = ParallelAgent([
    Agent(instructions="Get weather", tools=[weather_tool]),
    Agent(instructions="Get news", tools=[news_tool]),
])
```

## Loop — iterative refinement

Repeat a sub-agent until it calls `finish_loop()` or hits `max_iterations`. Use for draft → review → revise patterns.

```python
from apx_agent import LoopAgent

drafter = Agent(instructions="Draft a response. Call finish_loop when satisfied.")
refiner = LoopAgent(drafter, max_iterations=5)
```

## Router — conditional routing

LLM picks one branch based on the user's input. Use when the route is data-dependent but the branch agents are fixed.

```python
from apx_agent import RouterAgent

router = RouterAgent({
    "billing": billing_agent,
    "technical": tech_agent,
    "data": data_agent,
})
```

## Handoff — peer handoff mid-conversation

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

## `agent_tool` — LLM-driven delegation

When the parent agent's LLM should decide whether — and how many times — to delegate to a sub-agent, wrap the sub-agent as a tool. The parent calls it like any other tool, but the "tool" runs a full sub-agent loop and returns its final response. (This mirrors Google ADK's `AgentTool` pattern as a first-class composition primitive.)

```python
from apx_agent import Agent, agent_tool, lineage_tool, schema_tool

specialist = Agent(
    name="lineage_specialist",
    tools=[lineage_tool(), schema_tool()],
    instructions="You investigate Unity Catalog lineage and schemas.",
)

orchestrator = Agent(
    instructions=(
        "Answer the user's question. Delegate lineage and schema "
        "questions to the lineage specialist."
    ),
    tools=[
        agent_tool(
            specialist,
            name="ask_lineage_specialist",
            description=(
                "Investigate UC table lineage, upstream sources, and "
                "schemas. Call this when the user asks where data "
                "comes from or what columns exist."
            ),
        ),
    ],
)
```

**`name` and `description` are the routing contract.** They are what the parent LLM sees in the tool list — write them like tool docs: when to call, what it does, what it returns. Vague descriptions ("delegate to the specialist") leave the LLM guessing. `agent_tool` falls back to a generic delegate message if you omit `description`, but you should always set it explicitly for any real agent.

### Local or remote — same wrapper

`agent_tool` accepts any `BaseAgent`. For a remote sub-agent, construct a `RemoteDatabricksAgent` first; the wrapper doesn't change:

```python
from apx_agent import RemoteDatabricksAgent

# By Databricks App name (uses $DATABRICKS_HOST)
remote_billing = await RemoteDatabricksAgent.from_app_name("billing-agent")

# Or by full agent-card URL
remote_billing = await RemoteDatabricksAgent.from_card_url(
    "https://billing-agent.workspace.databricksapps.com/.well-known/agent.json"
)

orchestrator = Agent(tools=[
    agent_tool(
        remote_billing,
        name="billing",
        description="Answer billing and invoice questions for the calling user.",
    ),
])
```

`BaseAgent.run` is the only contract — the wrapper doesn't care whether the agent runs in-process or over HTTP. Identity passthrough flows through every form: in-process delegation, Model Serving sub-agent calls, and A2A app-to-app calls all preserve the calling user's OAuth token.

### `sub_agents=[url]` shorthand

When the remote agent's own `/.well-known/agent.json` discovery card already has good `name` and `description`, the `Agent` constructor accepts a `sub_agents=[...]` list of URLs. Each entry is auto-resolved to a `RemoteDatabricksAgent` and wrapped via `agent_tool` at startup:

```python
agent = Agent(
    tools=[run_sql_query, get_table_info],
    sub_agents=["https://data-inspector.workspace.databricksapps.com"],
)
```

Use explicit `agent_tool(...)` whenever the parent's calling context wants a different name or description than the remote agent self-describes.

### When to pick `agent_tool` vs `RouterAgent` vs `HandoffAgent`

All three are LLM-driven — the LLM picks the target. The differences are how often the LLM decides and who's in charge afterward:

| | Decision shape | Control after |
|---|---|---|
| `RouterAgent` | One — pick a branch from a closed set | Branch returns; routing is done |
| `HandoffAgent` | One — pick a peer from a closed set, transfer | Conversation moves to the peer; original agent is out |
| `agent_tool` | Many — call sub-agents like tools | Parent stays in charge, can call again, can interleave with its own tools |

Reach for `agent_tool` when the parent needs to stay in charge. Reach for `RouterAgent` when one decision is enough and the branches are mutually exclusive. Reach for `HandoffAgent` when control should fully transfer.

## Local vs remote — pick the deploy boundary

An agent is a *module* either way — the question is whether it lives in the same process as its caller, or in its own app behind A2A. That's an orthogonal choice from how the edge is selected (deterministic vs LLM-driven). Two axes, four corners:

|                       | Local (same process)                              | Remote (A2A)                                       |
|-----------------------|---------------------------------------------------|----------------------------------------------------|
| **Deterministic edge** | `SequentialAgent` / `ParallelAgent` / `LoopAgent` | `RemoteAgent` inside a workflow                    |
| **LLM-driven edge**    | `agent_tool(sub_agent)`                           | `sub_agents=[url]`                                 |

Pick the deploy boundary by **lifecycle and consumers**, not by agent count. Six agents that always run together, version together, and have no external caller belong in one app composed locally. One agent that has multiple callers, evolves independently, or has a different scaling shape belongs in its own app reached over A2A.

Reach for a separate app when at least one is true:

- **Second consumer.** Another agent (or another team) wants to call it.
- **Independent deploy cadence.** It changes on a different clock than its caller.
- **Different scaling profile.** CPU-heavy vs LLM-latency-bound, bursty vs steady, etc.
- **Cleaner OBO surface.** It owns a sensitive resource and the auth boundary should be explicit.

Keep it local otherwise.

`python/examples/data-triage-agent/` demonstrates both corners in one codebase: a six-step `SequentialAgent` composed locally (deterministic + local), delegating to a `data-inspector` sub-agent over A2A (LLM-driven + remote).

## Sub-agents — cross-endpoint composition

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

When deployed to Model Serving, sub-agent endpoints are auto-declared as resources. When hosted in Apps, sub-agent calls go through the app-to-app auth path (see [mcp-and-a2a.md](mcp-and-a2a.md)).

## Durable execution

`SequentialAgent`, `LoopAgent`, and `EvolutionaryAgent` can persist each step's output through a pluggable `WorkflowEngine` — a run can resume after a crash, redeploy, or pause.

| Backend | When to use |
|---------|-------------|
| `InMemoryEngine` | Default — tests, dev, short interactive runs |
| `DeltaEngine` | Production — SQL Statements API against a Delta table; survives restarts |
| `InngestEngine` | Optional adapter — when you already run Inngest as your orchestrator |

Durable workflows generally need Apps hosting — Model Serving is stateless and short-lived per request.
