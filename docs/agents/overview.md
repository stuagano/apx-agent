# Agents overview

An **agent** in apx-agent is a declaration: `instructions` + `tools` + a model name. The framework compiles it to a runnable service on Databricks (Apps or Model Serving) without changing the agent definition.

## Key concepts

| Concept | What it is |
|---------|-----------|
| `Agent` / `LlmAgent` | The core building block — a single LLM loop with tools, callbacks, and guardrails |
| `DataAgent` | `LlmAgent` pre-wired to a Unity Catalog schema: SQL, identity passthrough, optional Genie/Vector/UC-function tools |
| `CoworkerAgent` | `DataAgent` with persona, join key, and objective — the two-system join pattern |
| `SequentialAgent` | Multi-step pipeline — each step receives the previous step's output |
| `ParallelAgent` | Fan-out/gather — sub-agents run concurrently |
| `LoopAgent` | Iterative refinement — repeats until the sub-agent calls `finish_loop()` or hits `max_iterations` |
| `RouterAgent` | LLM-driven conditional routing — picks one branch from a closed set |
| `HandoffAgent` | LLM-driven peer handoff — transfers the conversation to a specialist mid-session |
| `agent_tool` | Wraps any agent as a callable tool so the parent LLM controls when and how often to delegate |
| `RemoteDatabricksAgent` | Wraps a deployed agent over HTTP — same interface as a local agent |

## Agent types

### Single-agent

Start here for almost everything. `Agent` (aliased from `LlmAgent`) is the leaf node in every composition pattern.

```python
from apx_agent import Agent

agent = Agent(
    name="my_agent",
    instructions="You are a helpful assistant.",
    tools=[my_tool],
)
```

The model is not a constructor argument — set it via `[tool.apx.agent]` in `pyproject.toml` (e.g. `model = "databricks-meta-llama-3-3-70b-instruct"`).

`DataAgent` is the recommended starting point when the agent needs to query Databricks data:

```python
from apx_agent import DataAgent

agent = DataAgent("main", "sales")
```

### Composition agents

Use composition when the orchestration structure is part of the contract — the graph topology is code, not LLM judgment.

| Agent | Use when |
|-------|---------|
| `SequentialAgent` | Step order is required (analyze → plan → execute) |
| `ParallelAgent` | Steps are independent and can run concurrently |
| `LoopAgent` | You need iterative refinement (draft → review → revise) |
| `agent_tool` | The parent LLM should decide whether and how often to delegate |

### Routing agents

Use routing when one message should go to one of several specialists.

| Agent | Use when |
|-------|---------|
| `RouterAgent` | One routing decision, then the branch runs independently |
| `HandoffAgent` | Control should transfer fully to the specialist mid-conversation |

## Choosing an agent type

```
Need to query Databricks data?
  └─ Yes → DataAgent (or CoworkerAgent for two-system joins)
  └─ No  → Agent

Need to combine multiple agents?
  ├─ Fixed order, sequential steps → SequentialAgent
  ├─ Independent parallel lookups  → ParallelAgent
  ├─ Iterative refinement loop     → LoopAgent
  ├─ LLM picks the target once     → RouterAgent or HandoffAgent
  └─ LLM decides when to delegate  → agent_tool
```

**RouterAgent vs HandoffAgent:** both pick a target with one LLM decision. Use `RouterAgent` when the branch returns and routing is done. Use `HandoffAgent` when the conversation should transfer to the specialist and the original agent exits.

**`agent_tool` vs routing:** Use `agent_tool` when the parent needs to stay in charge and may call the sub-agent multiple times or interleave with other tools. Use routing when one decision is enough and the branches are mutually exclusive.

## Running agents

Agents are invoked programmatically with `run_once`, or interactively via the CLI and dev UI:

```python
from apx_agent import run_once

# Single-turn (returns final assistant text)
result = run_once(agent, "What tables exist?")
```

`agent.run`/`agent.stream` exist too, but they take a `request` argument (they run inside the served FastAPI app). For script-level invocation outside an HTTP request, use `run_once`.

From the CLI:

```bash
uv run apx-agent agents run --reload        # local server with hot reload
```

Open `http://localhost:8000` for the browser-based dev UI with Chat, Traces, Topology, and Eval tabs.

## Identity passthrough

All agent types propagate the calling user's OAuth token through every tool call, sub-agent call, and A2A call. Unity Catalog grants are enforced at query time — the agent can only touch what the calling user can touch. No auth code at the tool level.

## Further reading

- [llm-agent.md](llm-agent.md) — `Agent` / `LlmAgent` full reference
- [composition.md](composition.md) — Sequential, Parallel, Loop, agent_tool
- [routing.md](routing.md) — RouterAgent, HandoffAgent
- [data-agent.md](data-agent.md) — DataAgent reference
- [coworker.md](coworker.md) — CoworkerAgent reference
- [../tools/overview.md](../tools/overview.md) — governed primitives: sql_tool, genie_tool, vector_search_tool, uc_function_tool
- [../multi-agent/overview.md](../multi-agent/overview.md) — local vs remote sub-agents, A2A auth
- [../get-started/quickstart.md](../get-started/quickstart.md) — scaffold, local run, first deploy
