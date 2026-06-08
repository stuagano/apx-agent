# Coming from ADK or OpenAI Agents SDK

apx-agent is structurally aligned with both Google ADK and the OpenAI Agents SDK. If you know either framework, you can be productive immediately. This page maps concepts across all three.

---

## Concept mapping

| Concept | ADK | OpenAI Agents SDK | apx-agent |
|---------|-----|-------------------|-----------|
| Agent definition | `LlmAgent` | `Agent` | `Agent` (alias: `LlmAgent`) |
| Run an agent | `runner.run()` | `Runner.run()` | `agent.run(messages)` — no Runner class |
| System prompt | `instruction=` | `instructions=` | `instructions=` (also accepts `instruction=`) |
| Tool decorator | `@FunctionTool` / `tool_function` | `@function_tool` | `@tool` |
| Tool list | `tools=[...]` | `tools=[...]` | `tools=[...]` |
| LLM model | `model=` in `LlmAgent` | `model=` in `Agent` | `model=` in `Agent` |
| Callbacks — before tool | `before_tool_callback` | `@tool_input_guardrail` | `before_tool` / `before_tool_callback` |
| Callbacks — after tool | `after_tool_callback` | _(output guardrail)_ | `after_tool` / `after_tool_callback` |
| Callbacks — before model | `before_model_callback` | `@input_guardrail` | `before_model` / `before_model_callback` |
| Callbacks — after model | `after_model_callback` | `@output_guardrail` | `after_model` / `after_model_callback` |
| Raise to abort | Raise from callback | `tripwire_triggered=True` | Raise from callback |
| Handoffs | Agent-to-agent transfer | `handoffs=[...]` / `@output_type` | `HandoffAgent` |
| Conditional routing | `LlmAgent` w/ routing instructions | handoffs / triage agent pattern | `RouterAgent` |
| Sequential pipeline | `SequentialAgent` | Agents as tools (chained) | `SequentialAgent` |
| Parallel execution | `ParallelAgent` | Parallel `Runner.run()` calls | `ParallelAgent` |
| Retry/loop | `LoopAgent` | _(manual)_ | `LoopAgent` |
| Wrap agent as tool | `AgentTool` | `agents_as_tools(...)` | `agent_tool(sub_agent)` |
| Sessions (in-conversation) | `Session` + `SessionService` | `session` strategy / `conversation_id` | `SessionStore` — pass `session_id` in `custom_inputs` |
| Cross-session memory | `MemoryService` | `to_input_list()` + external store | `MemoryBank` — `make_memory_tools` or `assemble_memory_context` |
| Deploy agent | _(separate infra)_ | _(separate infra)_ | `apx deploy` — Apps or Model Serving |
| Scaffold new agent | _(manual)_ | _(manual)_ | `apx scaffold my-agent` |
| Governed data access | _(manual)_ | _(manual)_ | `DataAgent`, `genie_tool`, `uc_function_tool` |
| Two-system join pattern | _(manual)_ | _(manual)_ | `CoworkerAgent` |
| Identity passthrough | _(manual)_ | _(manual)_ | OBO token — see [identity-passthrough.md](../safety/identity-passthrough.md) |

---

## No Runner class

**OpenAI Agents SDK developers:** There is no `Runner` class in apx-agent. Call `agent.run()` directly:

```python
# OpenAI Agents SDK
from agents import Agent, Runner
result = await Runner.run(agent, input="What tables exist?")

# apx-agent — same result, no Runner
from apx_agent import Agent
result = await agent.run([{"role": "user", "content": "What tables exist?"}])
```

For multi-turn sessions, pass a `session_id` in `custom_inputs` — the framework handles history loading and persistence automatically.

---

## `@tool` is `@function_tool` (OpenAI) and `FunctionTool` (ADK)

```python
# OpenAI Agents SDK
from agents import function_tool

@function_tool
def get_order_status(order_id: str) -> str:
    """Return the current status of an order."""
    ...

# ADK
from google.adk.tools import FunctionTool
def get_order_status(order_id: str) -> str:
    """Return the current status of an order."""
    ...
tool = FunctionTool(func=get_order_status)

# apx-agent — same decorator as OpenAI, same semantics as ADK
from apx_agent import tool

@tool
def get_order_status(order_id: str) -> str:
    """Return the current status of an order."""
    ...
```

Type hints become the LLM-visible parameter schema. The docstring becomes the tool description. Both frameworks — same behavior.

apx-agent adds `@tool(uc=..., grant=[...])` to sync the function to Unity Catalog at deploy time, making it callable from Genie, Managed MCP, and other agents browsing the catalog. See [custom-tools.md](../tools/custom-tools.md).

---

## Callbacks vs guardrails

ADK and apx-agent use the raise-to-abort callback pattern. OpenAI Agents SDK uses a `tripwire_triggered` boolean with decorator-based guardrails. apx-agent uses the ADK pattern — raise from the callback to block:

```python
# OpenAI Agents SDK pattern (NOT used in apx-agent)
@input_guardrail
async def check_pii(ctx, agent, input) -> GuardrailFunctionOutput:
    has_pii = "ssn" in input.lower()
    return GuardrailFunctionOutput(output_info=None, tripwire_triggered=has_pii)

# apx-agent pattern (also matches ADK)
def check_pii(prompts) -> None:
    if "ssn" in str(prompts).lower():
        raise PermissionError("PII guardrail: SSN detected")

agent = Agent(instructions="...", before_model=check_pii)
```

Both short names (`before_tool`, `after_tool`, `before_model`, `after_model`) and ADK long names (`before_tool_callback`, `after_tool_callback`, `before_model_callback`, `after_model_callback`) are accepted. See [callbacks.md](../safety/callbacks.md).

---

## Sessions

All three frameworks support multi-turn conversations — the wiring differs:

```python
# OpenAI Agents SDK — application-managed via to_input_list()
history = []
result1 = await Runner.run(agent, "First message")
history = result1.to_input_list() + [{"role": "user", "content": "Second message"}]
result2 = await Runner.run(agent, history)

# ADK — SessionService manages CRUD
session = session_service.create_session(app_name="my-app", user_id="user:alice")
runner.run(user_id="user:alice", session_id=session.id, new_message=content)

# apx-agent — pass session_id; the store handles the rest
chat.predict(
    messages=[ChatAgentMessage(role="user", content="Second message")],
    custom_inputs={"session_id": "user:alice:thread-42"},
)
```

Store options: `InMemorySessionStore` (dev), `DeltaSessionStore` (UC-governed Delta), `LakebaseSessionStore` (low-latency Postgres). See [sessions-and-memory.md](../running/sessions-and-memory.md).

---

## Memory

ADK `MemoryService` and apx-agent `MemoryBank` are both cross-session knowledge stores with semantic retrieval. The OpenAI SDK has no built-in equivalent — it leaves cross-session state to the application.

```python
# ADK — MemoryService with built-in tools
memory_service.add_session_to_memory(session)  # extract + store
# PreloadMemory tool auto-retrieves each turn in the agent

# apx-agent — MemoryStore with make_memory_tools
store.add(principal_id="user:alice", content="...", namespace="profile")
agent = Agent(
    instructions="...",
    tools=[*make_memory_tools(store, principal_id_resolver=lambda ctx: ctx.user_id)],
)
```

`make_memory_tools` returns `recall` / `remember` / `forget` tools. `assemble_memory_context` returns a markdown block for deterministic pre-loading — equivalent to ADK's `PreloadMemory`. See [sessions-and-memory.md](../running/sessions-and-memory.md).

---

## Deployment — Databricks-specific

Neither ADK nor the OpenAI Agents SDK has a built-in deploy story. apx-agent adds `apx deploy` to ship to Databricks Apps or Model Serving from a single command:

```bash
# apx-agent — no equivalent in ADK or OpenAI SDK
apx scaffold my-agent       # creates project structure, bakes UC schema
apx run                     # local dev server with hot reload
apx deploy                  # bundles + deploys to Databricks Apps
apx deploy --target model-serving --name main.agents.my_agent
```

See [quickstart.md](quickstart.md) for the full flow from scaffold to live endpoint.

---

## Databricks-specific additions

These have no equivalent in ADK or the OpenAI Agents SDK:

| Feature | Description |
|---------|-------------|
| `DataAgent` | Pre-grounded agent over a Unity Catalog schema — knows tables and columns without runtime discovery |
| `CoworkerAgent` | Two-system join pattern — joins two UC-governed sources on a shared business entity |
| `genie_tool("space_id")` | Natural-language SQL over a Genie space |
| `uc_function_tool(...)` | Calls a Unity Catalog SQL function as a tool |
| `vector_search_tool(...)` | Databricks Vector Search index as a retrieval tool |
| OBO identity passthrough | The calling user's OAuth token is forwarded to all tool calls, UC functions, and sub-agents — no service principal needed |
| `apx deploy` | One-command deploy to Databricks Apps or Model Serving |
| `apx scaffold` | Interactive project generation with UC schema baking |
| `apx publish-tools` | Syncs `@tool(uc=...)` functions to Unity Catalog |

See [data-agent.md](../agents/data-agent.md), [coworker.md](../agents/coworker.md), [tools/overview.md](../tools/overview.md), and [identity-passthrough.md](../safety/identity-passthrough.md).
