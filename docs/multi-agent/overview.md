# Multi-agent systems

Cross-process and cross-endpoint composition: when to split agents into separate apps, how to connect them, and how durable workflows span restarts.

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

When deployed to Model Serving, sub-agent endpoints are auto-declared as resources. When hosted in Apps, sub-agent calls go through the app-to-app auth path (see [a2a.md](a2a.md)).

## Durable execution

`SequentialAgent`, `LoopAgent`, and `EvolutionaryAgent` can persist each step's output through a pluggable `WorkflowEngine` — a run can resume after a crash, redeploy, or pause.

| Backend | When to use |
|---------|-------------|
| `InMemoryEngine` | Default — tests, dev, short interactive runs |
| `DeltaEngine` | Production — SQL Statements API against a Delta table; survives restarts |
| `InngestEngine` | Optional adapter — when you already run Inngest as your orchestrator |

Durable workflows generally need Apps hosting — Model Serving is stateless and short-lived per request.
