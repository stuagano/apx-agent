# Coming from ADK or OpenAI Agents SDK

apx-agent is structurally aligned with both Google ADK and the OpenAI Agents SDK. If you know either framework, you can be productive immediately. This page maps concepts across all three.

---

## Concept mapping

| Concept | ADK | OpenAI Agents SDK | apx-agent |
|---------|-----|-------------------|-----------|
| Agent definition | `LlmAgent` | `Agent` | `Agent` (alias: `LlmAgent`) |
| Run an agent | `runner.run()` | `Runner.run()` | `run_once(agent, prompt)` — no Runner class |
| System prompt | `instruction=` | `instructions=` | `instructions=` (also accepts `instruction=`) |
| Tool decorator | `@FunctionTool` / `tool_function` | `@function_tool` | `@tool` |
| Tool list | `tools=[...]` | `tools=[...]` | `tools=[...]` |
| LLM model | `model=` in `LlmAgent` | `model=` in `Agent` | `[tool.apx.agent].model` in TOML (or `model=` on `run_once` / `compile_to_chat_agent`) |
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
| Sessions (in-conversation) | `Session` + `SessionService` | `session` strategy / `conversation_id` | `ConversationStore` — pass `session_id` in `custom_inputs` |
| Cross-session memory | `MemoryService` | `to_input_list()` + external store | `MemoryStore` — `make_memory_tools` or `assemble_memory_context` |
| Deploy agent | _(separate infra)_ | _(separate infra)_ | `apx-agent agents deploy` — Apps or Model Serving |
| Scaffold new agent | _(manual)_ | _(manual)_ | `apx-agent agents scaffold my-agent` writes `my-agent.yaml` by default |
| Governed data access | _(manual)_ | _(manual)_ | `DataAgent`, `genie_tool`, `uc_function_tool` |
| Two-system join pattern | _(manual)_ | _(manual)_ | `CoworkerAgent` |
| Identity passthrough | _(manual)_ | _(manual)_ | OBO token — see [identity-passthrough.md](../safety/identity-passthrough.md) |

---

## No Runner class

**OpenAI Agents SDK developers:** There is no `Runner` class in apx-agent. Call `run_once()` directly:

```python
# OpenAI Agents SDK
from agents import Agent, Runner
result = await Runner.run(agent, input="What tables exist?")

# apx-agent — same result, no Runner
from apx_agent import Agent, run_once
result = run_once(agent, "What tables exist?")
```

For multi-turn sessions, pass a `session_id` in `custom_inputs` — the framework handles history loading and persistence automatically.

---

## Streaming and iteration limits

`run_once()` returns the final text; the compiled chat agent's `predict_stream()` yields chunks as they arrive:

```python
# apx-agent — streaming
from apx_agent import compile_to_chat_agent
from mlflow.types.agent import ChatAgentMessage

chat = compile_to_chat_agent(agent, model="databricks-claude-sonnet-4-6")
for chunk in chat.predict_stream(
    messages=[ChatAgentMessage(role="user", content="Explain the schema.")]
):
    print(chunk.delta.content, end="", flush=True)
```

| ADK | OpenAI Agents SDK | apx-agent |
|-----|-------------------|-----------|
| runner iteration limit | `MaxTurnsExceeded` / `max_turns` | `max_iterations` (default 10) |
| `adk run` / `adk web` | _(no built-in runner CLI)_ | `apx-agent agents run` (dev server), `apx-agent eval test --prompt "..."` |

`max_iterations` caps the tool-calling loop — the agent stops when it's reached. Lower it for cost-sensitive agents, raise it for complex multi-step work.

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

## Handoffs and composition

| ADK | OpenAI Agents SDK | apx-agent |
|-----|-------------------|-----------|
| `AgentTool` | `handoff(agent)` / `handoffs=[...]` | `HandoffAgent`, `agent_tool(agent)` |
| `SequentialAgent` / `ParallelAgent` / `LoopAgent` | _(manual)_ | `SequentialAgent` / `ParallelAgent` / `LoopAgent` |
| routing-instruction agent | triage agent pattern | `RouterAgent` (description-driven) |

```python
# OpenAI Agents SDK
from agents import Agent, handoff
triage = Agent(name="triage", handoffs=[billing_agent, support_agent])

# apx-agent equivalent
from apx_agent import HandoffAgent
triage = HandoffAgent(
    name="triage",
    instructions="Route to billing or support based on the question.",
    agents=[billing_agent, support_agent],
)
```

See [routing.md](../agents/routing.md) and [composition.md](../agents/composition.md).

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

Store options: `InMemoryConversationStore` (dev), `LakebaseConversationStore` (UC-governed, low-latency Postgres). See [sessions-and-memory.md](../running/sessions-and-memory.md).

---

## Memory

ADK `MemoryService` and apx-agent `MemoryStore` are both cross-session knowledge stores with semantic retrieval. The OpenAI SDK has no built-in equivalent — it leaves cross-session state to the application.

```python
# ADK — MemoryService with built-in tools
memory_service.add_session_to_memory(session)  # extract + store
# PreloadMemory tool auto-retrieves each turn in the agent

# apx-agent — MemoryStore with make_memory_tools
store.add({"principal_id": "user:alice", "content": "...", "namespace": "profile"})
agent = Agent(
    instructions="...",
    tools=[*make_memory_tools(store, principal_id_resolver=lambda: "user:alice")],
)
```

`make_memory_tools` returns `recall` / `remember` / `forget` tools. `assemble_memory_context` returns a markdown block for deterministic pre-loading — equivalent to ADK's `PreloadMemory`. See [sessions-and-memory.md](../running/sessions-and-memory.md).

---

## Observability and tracing

| ADK | OpenAI Agents SDK | apx-agent |
|-----|-------------------|-----------|
| `adk web` traces view | built-in tracing, 25+ integrations | MLflow autolog + `/_apx/traces` dev UI |
| structured span output | `trace_id` / `group_id` on `RunConfig` | `apx.*` span attributes on every trace |
| `adk eval` | _(none)_ | `apx-agent eval run evalset.jsonl` (LLM-as-judge) |

Tracing is on by default — every run produces an MLflow span tree (LLM call, tool call, SQL, response):

- **Dev UI:** `/_apx/traces` (e.g. `http://localhost:8000/_apx/traces`)
- **CLI:** `apx-agent traces list --agent <name>`
- **Export:** `apx-agent traces export --table <catalog.schema.table> --hours 24`
- **Workspace:** Machine Learning → Experiments → your agent's experiment

---

## Deployment — Databricks-specific

Neither ADK nor the OpenAI Agents SDK has a built-in deploy story. apx-agent adds `apx-agent agents deploy` to ship to Databricks Apps or Model Serving from a single command:

```bash
# apx-agent — no equivalent in ADK or OpenAI SDK
apx-agent agents scaffold my-agent   # writes my-agent.yaml
apx-agent agents deploy my-agent.yaml --target apps  # generates project + deploys
apx-agent agents scaffold my-agent --no-yaml         # optional local project dir
cd my-agent && apx-agent agents run                  # local dev server with hot reload
apx-agent agents deploy --target model-serving --name main.agents.my_agent
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
| `apx-agent agents deploy` | One-command deploy to Databricks Apps or Model Serving |
| `apx-agent agents scaffold` | YAML-first scaffold; add `--no-yaml` for project generation |
| `apx-agent uc publish` | Syncs `@tool(uc=...)` functions to Unity Catalog |

See [data-agent.md](../agents/data-agent.md), [coworker.md](../agents/coworker.md), [tools/overview.md](../tools/overview.md), and [identity-passthrough.md](../safety/identity-passthrough.md).

---

## Full parameter mapping

| ADK / OpenAI param | apx-agent param | Notes |
|--------------------|-----------------|-------|
| `instruction` (ADK) | `instructions` | Both spellings accepted |
| `model` | `[tool.apx.agent].model` (or `model=` on `run_once` / `compile_to_chat_agent`) | Databricks serving endpoint name; not an `Agent` constructor arg |
| `tools` | `tools` | `@tool` functions, governed primitives, or `agent_tool(agent)` wrappers |
| `before_tool_callback` (ADK) | `before_tool_callback` or `before_tool` | Both accepted; ADK form takes precedence |
| `after_tool_callback` (ADK) | `after_tool_callback` or `after_tool` | Both accepted |
| `before_model_callback` (ADK) | `before_model_callback` or `before_model` | Both accepted |
| `input_guardrails` (OpenAI) | `input_guardrails` | Same parameter name |
| `output_guardrails` (OpenAI) | `output_guardrails` | Same parameter name |
| `handoffs` (OpenAI) | `agents` on `HandoffAgent` | |
| `max_turns` (OpenAI) | `max_iterations` | Default 10 |
| `description` | `description` | Used in routing context and A2A discovery |
