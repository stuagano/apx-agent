# LlmAgent

`LlmAgent` (aliased as `Agent`) is the core building block — a single LLM loop with tools, callbacks, and guardrails. Every other agent type either wraps or composes `LlmAgent` instances.

## Core ideas

- **`instructions`** is the system prompt, prepended to every conversation turn.
- **`tools`** is a list of Python functions, tool objects, or `agent_tool(...)` wrappers. The LLM calls them in a loop until it produces a final answer.
- **`description`** is the one-line summary used in routing context (RouterAgent/HandoffAgent) and A2A discovery cards. Write it like tool documentation — when to invoke this agent, what it knows.
- **`model`** names the LLM endpoint in your Databricks workspace. It is not a constructor argument — set it via `[tool.apx.agent]` in `pyproject.toml`. Swap the endpoint to change the underlying model without changing anything else.
- **`max_iterations`** is a safety cap on tool-calling loops. It defaults to `None`, in which case LangGraph's own recursion limit applies (apx adds no cap of its own); set an integer (including `0`) to bound the loop explicitly. `0` is a real cap (`recursion_limit=1`), not “unlimited.”

## Minimal example

```python
from apx_agent import Agent

agent = Agent(
    name="my_agent",
    instructions="You are a helpful assistant.",
    tools=[my_tool],
)
```

The model is set via `[tool.apx.agent]` in `pyproject.toml`, not on the constructor:

```toml
[tool.apx.agent]
model = "databricks-meta-llama-3-3-70b-instruct"
```

## Parameters

| Parameter | Alias | Type | Description |
|-----------|-------|------|-------------|
| `name` | — | `str` | Agent identifier; used in routing, topology views, and A2A discovery cards |
| `instructions` | `instruction` | `str` | System prompt prepended to every conversation |
| `description` | — | `str` | One-line summary used by RouterAgent/HandoffAgent and A2A discovery |
| `tools` | — | `list` | Tool functions, tool objects, or `agent_tool(...)` wrappers |
| `temperature` | — | `float \| None` | Sampling temperature; `None` uses the model default |
| `max_tokens` | — | `int \| None` | Max output tokens; `None` uses the model default |
| `max_iterations` | — | `int \| None` | Safety cap on tool-calling loops; `None` (default) defers to LangGraph's recursion limit |
| `memory` | — | `str` | Memory tier: `"off"`, `"inmemory"`, or `"persistent"` |
| `output_schema` | — | `type[BaseModel] \| None` | Pydantic model class for strict validation of the final JSON answer; `None` keeps free-text output |
| `output_key` | — | `str \| None` | Named shared-state slot for the final text, or JSON-compatible validated data when `output_schema` is set |
| `sub_agents` | — | `list[str]` | URLs of remote agents auto-wrapped as `agent_tool` at startup |
| `tool_loading` | — | `"eager"` \| `"deferred"` | How tools are advertised to the model. `"eager"` (default) binds every author tool on the first hop. `"deferred"` sends only a client-side `tool_search` tool first; matched tools bind for later hops. Not Anthropic's server-side tool-search beta. |

With `tool_loading="deferred"`, the first compiled model call advertises only `tool_search`. The model searches the agent's catalog by name/description; matching tools stay bound for later hops in that session. Loop `finish_loop` and Handoff `transfer_to_*` stay always visible — they are orchestration, not inventory.

## Typed output

Declare `output_schema` when the final answer must satisfy a data contract:

```python
from pydantic import BaseModel, ConfigDict, Field
from apx_agent import Agent


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: int
    evidence: list[str] = Field(min_length=1)


extract = Agent(
    name="extract",
    instructions="Extract a finding from the supplied records.",
    output_schema=Finding,
    output_key="finding",
)
```

APX adds the model's JSON schema to the prompt and validates the completed answer
strictly. Numeric strings do not become numbers, and Markdown fences or prose
around JSON are rejected. The model's field constraints and extra-field policy
apply. Validation proves the declared shape, not the factual accuracy of evidence.

On success, `output_key` receives JSON-compatible data; it does not contain a
Pydantic instance. `run()` still returns a string containing the validated JSON,
and typed `stream()` output is buffered until validation. `output_key` is optional
for a standalone typed answer. See [typed step contracts](composition.md#typed-step-contracts)
for passing the value to another agent.

Object-shaped typed answers also carry a structured data part across supported
remote bindings. See [structured step payloads](../multi-agent/a2a.md#structured-step-payloads)
for wire formats, receiver validation, and transport limits.

Invalid output raises `OutputValidationError` before publishing the result or
calling `after_agent_callback`. It exposes `agent_name`, `output_key`, and
`errors` with validation locations and types, excluding raw input values and
validator messages. Input/output guardrail rejection also stops a typed agent;
replacement text is not accepted as a successful typed result. The sequence does
not advance, and typed runs do not replay tools through a streaming fallback.
Already completed external tool effects are not rolled back.

This version supports typed leaves in sequential graphs and standalone typed
agents. Parallel branches, loop bodies, and handoff members reject
`output_schema`. There is no schema option on the composite itself, and no retry,
JSON-repair, or escalation policy. Omitting `output_schema` preserves free-text
behavior. The [implementation contract](../design/keyed-shared-state.md#typed-output-contracts-835)
details execution and state semantics.

## Running agents

> **Coming from OpenAI Agents SDK?** There is no `Runner` class — call `run_once(agent, prompt)`. `await Runner.run(agent, input)` → `run_once(agent, "...")`.

For script-level invocation outside an HTTP request, use `run_once` — it returns the final assistant text:

```python
from apx_agent import run_once

# Single-turn (returns final assistant text)
result = run_once(agent, "What tables exist?")
```

`agent.run`/`agent.stream` also exist, but they take a `request` argument because they run inside the served FastAPI app. Reach for them only inside request handlers; for everything else, use `run_once`.

From the CLI, use `apx-agent agents run` for the local development server:

```bash
uv run apx-agent agents run --reload    # starts FastAPI on :8000 with hot reload
```

Open `http://localhost:8000` for the dev UI — Chat, Traces, Topology, and Eval tabs. See [../get-started/dev-ui.md](../get-started/dev-ui.md) for the full dev UI reference.

## Callbacks

Callbacks are lifecycle hooks called before and after each phase of the agent loop. See [../safety/callbacks.md](../safety/callbacks.md) for the full hook reference.

Both short names (`before_tool`, `after_tool`, `before_model`, `after_model`) and ADK-compatible long names (`before_tool_callback`, `after_tool_callback`, `before_model_callback`, `after_model_callback`) are accepted — they are the same hooks.

```python
agent = Agent(
    instructions="...",
    before_tool_callback=lambda name, args: print(f"calling tool: {name}"),
    after_model_callback=lambda resp: print("model response received"),
    before_agent_callback=lambda msgs: None,
    after_agent_callback=lambda text: None,
)
```

## Guardrails

Built-in guards restrict which tools the agent may call, enforce rate limits, and detect prompt injection. The constructor takes `input_guardrails=`/`output_guardrails=` (lists of functions); tool gating and rate limits are `before_tool` callbacks. See [../safety/compliance.md](../safety/compliance.md) for full configuration options.

```python
from apx_agent import (
    Agent, RateLimit, ToolAllowlist, prompt_injection_heuristic, compose,
)

agent = Agent(
    instructions="...",
    tools=[...],
    input_guardrails=[prompt_injection_heuristic()],
    before_tool=compose(
        RateLimit(per_minute=60),
        ToolAllowlist({"sql_query", "get_schema"}),
    ),
)
```

The declarative `GuardrailsConfig` (with `allowed_tools`, `rate_limit`, `injection_detection`) is applied via `[tool.apx.agent]` config, not passed to the constructor. See [../reference/configuration.md](../reference/configuration.md).

## Remote sub-agents

`sub_agents=[url, ...]` is a shorthand that auto-resolves each URL to a `RemoteDatabricksAgent` and wraps it via `agent_tool` at startup. Use this when the remote agent's own discovery card already has good `name` and `description`.

```python
agent = Agent(
    tools=[run_sql_query, get_table_info],
    sub_agents=["https://data-inspector.workspace.databricksapps.com"],
)
```

Use explicit `agent_tool(...)` when you want to override the remote agent's `name` or `description` in the parent's calling context. See [composition.md](composition.md) for details.

## Composition

`LlmAgent` is the leaf node in every composition pattern:

- **[composition.md](composition.md)** — SequentialAgent, ParallelAgent, LoopAgent, agent_tool
- **[routing.md](routing.md)** — RouterAgent, HandoffAgent
- **[../multi-agent/overview.md](../multi-agent/overview.md)** — RemoteDatabricksAgent, cross-process deployment, A2A auth
