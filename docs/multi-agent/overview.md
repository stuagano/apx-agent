# Multi-agent systems

Cross-process and cross-endpoint composition: when to split agents into separate apps, how to connect them, and how durable workflows span restarts.

> **Coming from ADK?** apx-agent uses the same structural primitives — `SequentialAgent`, `ParallelAgent`, `LoopAgent` for local composition; remote agents via `sub_agents=[url]` or `agent_tool(sub_agent)`. There is no `Pipeline` wrapper — you compose agents directly. See [migration guide](../get-started/migration.md) for a full concept map.
>
> **Coming from OpenAI Agents SDK?** "Handoffs" in the OpenAI SDK map to `HandoffAgent`. There is no `Runner` class — call `agent.run()` directly. `sub_agents=[url]` is the equivalent of passing remote agents as handoff targets. See [migration guide](../get-started/migration.md) for a full concept map.

---

## Local vs remote — pick the deploy boundary

An agent is a *module* either way — the question is whether it lives in the same process as its caller, or in its own app behind A2A. That is an orthogonal choice from how the edge is selected (deterministic vs LLM-driven):

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

---

## Handoffs — LLM-driven peer transfer

A **handoff** transfers the conversation from one agent to another mid-session. The receiving agent inherits the conversation state. This is the apx-agent equivalent of OpenAI Agents SDK handoffs and ADK agent-to-agent transfer.

```python
from apx_agent import HandoffAgent, Agent

triage_agent = Agent(
    name="triage",
    description="First contact — classify and route the user's request.",
    tools=[],
)
billing_agent = Agent(
    name="billing",
    description="Handles billing inquiries, invoice lookups, and payment issues.",
    tools=[...],
)
tech_agent = Agent(
    name="technical",
    description="Handles technical support, error messages, and configuration issues.",
    tools=[...],
)

# Pass agents as a list; HandoffAgent uses each agent's name as the transfer key
triage = HandoffAgent(
    agents=[triage_agent, billing_agent, tech_agent],
    # start defaults to "triage" (first agent's name)
)
```

Each agent's `description` becomes the transfer tool description visible to the LLM. When the triage agent decides to hand off, it calls a generated tool (`transfer_to_billing`, `transfer_to_technical`) — the framework then routes control to that agent for the remainder of the session.

**`RouterAgent` vs `HandoffAgent`:**

| | `RouterAgent` | `HandoffAgent` |
|---|---|---|
| Decision shape | Pick one branch from a closed set | Pick a peer, transfer |
| Control after | Branch returns; routing is done | Conversation moves to peer; original agent is out |
| ADK analogy | `LlmAgent` with routing instructions | ADK agent-to-agent transfer |
| OpenAI analogy | Agent with `output_type` selector | OpenAI SDK handoff |

For `RouterAgent` and `KeywordRouter`, see [routing.md](../agents/routing.md).

---

## Sub-agents — cross-endpoint composition

When sub-agents are deployed as separate Model Serving endpoints or Databricks Apps, declare them with `sub_agents`:

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

When deployed to Model Serving, sub-agent endpoints are auto-declared as resources. When hosted in Apps, sub-agent calls go through the app-to-app auth path — see [a2a.md](a2a.md).

---

## Local composition — deterministic pipelines

For deterministic multi-step flows in the same process, use the composition primitives:

| Primitive | Pattern |
|-----------|---------|
| `SequentialAgent` | Run agents in order; each receives prior output |
| `ParallelAgent` | Run agents concurrently; collect all outputs |
| `LoopAgent` | Run an agent repeatedly until a stop condition |
| `agent_tool(sub_agent)` | Wrap an agent as a tool for another agent |

See [composition.md](../agents/composition.md) for the full reference with examples.

---

## Durable execution

`SequentialAgent`, `LoopAgent`, and `EvolutionaryAgent` can persist each step's output through a pluggable `WorkflowEngine` — a run can resume after a crash, redeploy, or pause.

| Backend | When to use |
|---------|-------------|
| `InMemoryEngine` | Default — tests, dev, short interactive runs |
| `DeltaEngine` | Production — SQL Statements API against a Delta table; survives restarts |
| `InngestEngine` | Optional adapter — when you already run Inngest as your orchestrator |

Durable workflows generally need Apps hosting — Model Serving is stateless and short-lived per request.

---

## Decision guide

| Goal | Approach |
|------|----------|
| Classify and route a request | `RouterAgent` — LLM picks one branch |
| Transfer conversation to a specialist | `HandoffAgent` — peer transfer mid-session |
| Fixed pipeline (step A then B then C) | `SequentialAgent` — local, deterministic |
| Parallel data gathering | `ParallelAgent` — concurrent, local |
| Agent callable like a tool | `agent_tool(sub_agent)` — parent stays in control |
| Separate app, different team | `sub_agents=[url]` — A2A, remote |
| Survive restarts / long-running | `DeltaEngine` durable execution |
