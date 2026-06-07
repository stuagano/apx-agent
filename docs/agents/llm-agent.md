# LlmAgent

`LlmAgent` (aliased as `Agent`) is the core building block — a single LLM loop with tools, callbacks, and guardrails.

## Constructor

```python
from apx_agent import Agent

agent = Agent(
    name="my_agent",
    instructions="You are a helpful assistant.",   # system prompt
    tools=[my_tool],
    model="databricks-meta-llama-3-3-70b-instruct",
    temperature=0.0,
    max_tokens=2048,
    max_iterations=10,
)
```

| Parameter | ADK alias | Type | Description |
|-----------|-----------|------|-------------|
| `name` | — | `str` | Agent identifier; used in routing, topology, and discovery cards |
| `instructions` | `instruction` | `str` | System prompt prepended to every conversation |
| `description` | — | `str` | One-line summary used in routing context and A2A discovery |
| `tools` | — | `list` | Tool functions, tool objects, or `agent_tool(...)` wrappers |
| `model` | — | `str` | LLM endpoint name |
| `temperature` | — | `float \| None` | Sampling temperature; `None` uses model default |
| `max_tokens` | — | `int \| None` | Max output tokens; `None` uses model default |
| `max_iterations` | — | `int` | Safety cap on tool-calling loops (default: 10) |
| `memory` | — | `str \| MemoryBackendConfig \| None` | Memory tier: `"off"`, `"inmemory"`, `"persistent"` |
| `sub_agents` | — | `list[str]` | URLs of remote agents auto-wrapped as `agent_tool` at startup |

## Callbacks

See [../safety/callbacks.md](../safety/callbacks.md) for the full hook reference.

```python
agent = Agent(
    instructions="...",
    before_tool_callback=lambda name, args: print(f"tool: {name}"),
    after_model_callback=lambda resp: print(f"model done"),
    before_agent_callback=lambda msgs: None,
    after_agent_callback=lambda text: None,
)
```

## Guardrails

See [../safety/compliance.md](../safety/compliance.md) for built-in guard configuration.

```python
from apx_agent import Agent
from apx_agent._models import GuardrailsConfig

agent = Agent(
    instructions="...",
    guardrails=GuardrailsConfig(
        allowed_tools=["sql_query", "get_schema"],
        rate_limit=60,
        injection_detection=True,
    ),
)
```

## Running

```python
# Single-turn (returns final text)
result = await agent.run([{"role": "user", "content": "What tables exist?"}])

# Streaming (yields chunks)
async for chunk in agent.stream([{"role": "user", "content": "Explain the schema."}]):
    print(chunk, end="", flush=True)
```

## Composition

`LlmAgent` is the leaf node in every composition pattern:

- **[composition.md](composition.md)** — Sequential, Parallel, Loop, agent_tool
- **[routing.md](routing.md)** — RouterAgent, HandoffAgent
- **[../multi-agent/overview.md](../multi-agent/overview.md)** — cross-process deployment
