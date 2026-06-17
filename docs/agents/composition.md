# Agent composition

Composable agent patterns where the orchestration structure is part of the contract — the LLM executes within each step, but the graph topology is code.

## Core ideas

- **Sequential, Parallel, and Loop agents** give you deterministic structure. The topology is fixed at definition time; the LLM operates within each node.
- **`agent_tool`** inverts this: the parent LLM decides whether to delegate and may call the sub-agent multiple times in the same turn.
- Use composition when the structure is the contract. Use `RouterAgent` or `HandoffAgent` (see [routing.md](routing.md)) when you want the LLM to pick the target from a closed set.

| Agent | Purpose |
|-------|---------|
| `SequentialAgent` | Pipeline execution (analyze → plan → execute) |
| `ParallelAgent` | Fan-out / gather (fetch data from multiple sources concurrently) |
| `LoopAgent` | Iterative refinement (draft → review → revise until done) |
| `agent_tool` | LLM-driven delegation — wrap any agent as a callable tool |

## SequentialAgent — multi-step pipelines

Each step receives the previous step's output as context. Use when step order is part of the contract.

```python
from apx_agent import Agent, SequentialAgent, lineage_tool, schema_tool

pipeline = SequentialAgent([
    Agent(
        instructions="Identify which tables the user is asking about.",
        tools=[lineage_tool(), schema_tool()],
    ),
    Agent(instructions="Plan a multi-step investigation."),
    Agent(instructions="Execute the plan and report findings."),
])
```

## ParallelAgent — fan-out / gather

Run sub-agents concurrently and merge results. Use for independent lookups that don't depend on each other.

```python
from apx_agent import ParallelAgent, Agent

merged = ParallelAgent([
    Agent(instructions="Get weather data.", tools=[weather_tool]),
    Agent(instructions="Get news headlines.", tools=[news_tool]),
])
```

## LoopAgent — iterative refinement

Repeat a sub-agent until it calls `finish_loop()` or hits `max_iterations`. Use for draft → review → revise patterns.

```python
from apx_agent import Agent, LoopAgent

drafter = Agent(instructions="Draft a response. Call finish_loop when satisfied.")
refiner = LoopAgent(drafter, max_iterations=5)
```

## `agent_tool` — LLM-driven delegation

When the parent LLM should decide whether — and how many times — to delegate to a sub-agent, wrap the sub-agent as a tool. The parent calls it like any other tool; the "tool" runs a full sub-agent loop and returns its final response. (This mirrors Google ADK's `AgentTool` pattern as a first-class composition primitive.)

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

**`name` and `description` are the routing contract.** They are what the parent LLM sees in the tool list — write them like tool docs: when to call, what it does, what it returns. Vague descriptions leave the LLM guessing. `agent_tool` falls back to a generic delegate message if you omit `description`, but you should always set it explicitly for any real agent.

### Local or remote — same wrapper

`agent_tool` accepts any `BaseAgent`. For a remote sub-agent, construct a `RemoteDatabricksAgent` first; the wrapper interface doesn't change.

```python
from apx_agent import RemoteDatabricksAgent, Agent, agent_tool

# By Databricks App name (resolves against $DATABRICKS_HOST)
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

`BaseAgent.run` is the only contract — the wrapper doesn't care whether the agent runs in-process or over HTTP. Identity passthrough (OBO token) flows through every form: in-process delegation, Model Serving sub-agent calls, and A2A app-to-app calls all preserve the calling user's OAuth token.

### `sub_agents=[url]` shorthand

When the remote agent's own `/.well-known/agent.json` discovery card already has good `name` and `description`, the `Agent` constructor accepts a `sub_agents=[...]` list of URLs. Each entry is auto-resolved to a `RemoteDatabricksAgent` and wrapped via `agent_tool` at startup.

```python
agent = Agent(
    tools=[run_sql_query, get_table_info],
    sub_agents=["https://data-inspector.workspace.databricksapps.com"],
)
```

Use explicit `agent_tool(...)` whenever the calling context wants a different `name` or `description` than the remote agent self-describes.

## Decision guide

| | Decision shape | Control after |
|---|---|---|
| `RouterAgent` | One — pick a branch from a closed set | Branch returns; routing is done |
| `HandoffAgent` | One — pick a peer from a closed set, transfer | Conversation moves to the peer; original agent exits |
| `agent_tool` | Many — call sub-agents like tools | Parent stays in charge; can call again and interleave with other tools |

Reach for `agent_tool` when the parent needs to stay in charge. Reach for `RouterAgent` when one decision is enough and the branches are mutually exclusive. Reach for `HandoffAgent` when control should fully transfer to the specialist.

See [routing.md](routing.md) for `RouterAgent` and `HandoffAgent` details.
