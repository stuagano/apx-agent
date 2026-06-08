# Migration guide

Coming from Google ADK or OpenAI Agents SDK? This page maps every concept you already know to its apx-agent equivalent.

---

## Agent construction

All three frameworks build an agent from instructions, tools, and a model name.

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `LlmAgent(name, instruction, tools, model)` | `Agent(name, instructions, tools, model)` | `LlmAgent(name, instructions, tools, model)` |
| `Agent` is the base class | `Agent` is the base class | `LlmAgent` and `Agent` are the same class |

```python
# Google ADK
from google.adk.agents import LlmAgent
agent = LlmAgent(name="helper", instruction="...", tools=[my_tool], model="gemini-2.0-flash")

# OpenAI Agents SDK
from agents import Agent
agent = Agent(name="helper", instructions="...", tools=[my_tool], model="gpt-4o")

# apx-agent
from apx_agent import Agent
agent = Agent(name="helper", instructions="...", tools=[my_tool], model="databricks-claude-sonnet-4-6")
```

**Databricks-specific additions:** apx-agent ships `DataAgent` (grounded in a UC schema) and `CoworkerAgent` (two source systems joined on a shared key). There is no ADK or OpenAI SDK equivalent for these — they are Databricks specializations on top of `LlmAgent`.

---

## Running an agent

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `runner.run_async(app_name, user_id, session_id, new_message)` | `Runner.run(agent, input)` / `Runner.run_sync(...)` / `Runner.run_streamed(...)` | `await agent.run(messages)` / `agent.stream(messages)` |
| Returns `RunResult` with `final_response` | Returns `RunResult` with `.final_output` | Returns final text string |
| runner enforces an iteration limit | `MaxTurnsExceeded` | `max_iterations` safety cap — agent stops after N tool-calling loops (default: 10) |
| `adk run` CLI, `adk web` UI | No built-in CLI runner | `apx run` (FastAPI dev server), `apx test --prompt "..."` |

```python
# apx-agent — single turn, async
result = await agent.run([{"role": "user", "content": "What tables exist?"}])

# apx-agent — streaming
async for chunk in agent.stream([{"role": "user", "content": "Explain the schema."}]):
    print(chunk, end="", flush=True)
```

The `max_iterations` parameter (default: 10) is the safety cap on tool-calling loops. When the cap is reached, the agent stops. Set it lower for cost-sensitive agents, higher for complex multi-step work.

---

## Defining tools

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `FunctionTool(fn)` or `@tool` | `@function_tool` | `@tool` |
| Tool docstring → description | Tool docstring → description | Tool docstring → description |
| Type hints → JSON Schema | Type hints → JSON Schema | Type hints → JSON Schema |

```python
# Google ADK
from google.adk.tools import tool
@tool
def lookup(account_id: str) -> str:
    """Look up an account."""
    return f"Account {account_id}"

# OpenAI Agents SDK
from agents import function_tool
@function_tool
def lookup(account_id: str) -> str:
    """Look up an account."""
    return f"Account {account_id}"

# apx-agent
from apx_agent import tool
@tool
def lookup(account_id: str) -> str:
    """Look up an account."""
    return f"Account {account_id}"
```

**Databricks-specific governed primitives** — no equivalent in ADK or OpenAI SDK:

| apx-agent | Description |
|---|---|
| `uc_function_tool("catalog.schema.fn")` | Calls a Unity Catalog SQL function |
| `genie_tool("space_id")` | Delegates to a Genie space for natural-language SQL |
| `vector_search_tool("index_name")` | Queries a Databricks Vector Search index |
| `sql_tool` | Direct SQL execution with UC grant enforcement |

---

## Guardrails

ADK and OpenAI use different patterns. apx-agent supports both.

### OpenAI Agents SDK pattern

OpenAI uses `@input_guardrail` / `@output_guardrail` decorators that return `GuardrailFunctionOutput(tripwire_triggered=bool)`. Exceptions `InputGuardrailTripwireTriggered` / `OutputGuardrailTripwireTriggered` are named.

apx-agent equivalent:

```python
# OpenAI Agents SDK
from agents import Agent, input_guardrail, GuardrailFunctionOutput

@input_guardrail
async def check_pii(ctx, agent, input):
    has_ssn = "ssn" in str(input).lower()
    return GuardrailFunctionOutput(tripwire_triggered=has_ssn)

agent = Agent(instructions="...", input_guardrails=[check_pii])

# apx-agent equivalent — raise to trigger; same input_guardrails parameter name
from apx_agent import Agent

def check_pii(messages):
    if "ssn" in str(messages).lower():
        raise PermissionError("PII guardrail: SSN detected")

agent = Agent(
    instructions="...",
    before_agent_callback=check_pii,
    # or: input_guardrails=[check_pii]  (same parameter name as OpenAI)
)
```

### ADK pattern

ADK uses `before_tool_callback` and `before_model_callback` (raise to block). apx-agent accepts both the short names and the ADK-compatible names.

```python
# ADK
agent = LlmAgent(
    instruction="...",
    before_tool_callback=lambda ctx, ti: raise_if_blocked(ti),
    before_model_callback=lambda ctx, req: raise_if_pii(req),
)

# apx-agent — both forms accepted; ADK form takes precedence when both are supplied
from apx_agent import Agent

agent = Agent(
    instructions="...",
    before_tool_callback=lambda name, args: check_tool(name, args),  # ADK name
    before_tool=lambda name, args: check_tool(name, args),           # short name (same hook)
    before_model_callback=lambda prompts: check_pii(prompts),
)
```

**Summary:**

| OpenAI / ADK | apx-agent |
|---|---|
| `@input_guardrail` → `GuardrailFunctionOutput(tripwire_triggered=True)` | `before_agent_callback` → raise `PermissionError` |
| `input_guardrails=[fn]` | `input_guardrails=[fn]` (same param name) |
| `OutputGuardrailTripwireTriggered` | raise in `after_agent_callback` or `output_guardrails=[fn]` |
| `before_tool_callback` (ADK) | `before_tool_callback` or `before_tool` (both accepted) |
| `before_model_callback` (ADK) | `before_model_callback` or `before_model` (both accepted) |

---

## Sessions

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `Session` + `SessionService` | `session` strategy, `conversation_id` | `session_id` passed per request |
| `InMemorySessionService`, `DatabaseSessionService` | In-memory or server-managed | `InMemorySessionStore`, `DeltaSessionStore`, `LakebaseSessionStore` |

Pass `session_id` as a `custom_inputs` key when calling the agent. The framework loads history before the LLM call, appends the new exchange, and persists it. Without a `session_id`, every call starts fresh.

See [running/sessions-and-memory.md](running/sessions-and-memory.md).

---

## Memory

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `MemoryService` (cross-session recall) | `to_input_list()` + external store | `MemoryBank` (semantic recall), `MemoryStore` |
| `InMemoryMemoryService`, `VertexAiRagMemoryService` | — | `InMemoryMemoryStore`, `LakebaseMemoryStore` |
| `ExampleProvider` (few-shot) | — | `ExampleStore` (few-shot from real session history) |

The `memory=` parameter on `LlmAgent` or `DataAgent` is the shorthand:

```python
agent = Agent(instructions="...", memory="persistent")   # persistent store (Delta-backed by default)
agent = Agent(instructions="...", memory="inmemory")     # dev/test
agent = Agent(instructions="...", memory="off")          # no memory
```

See [running/sessions-and-memory.md](running/sessions-and-memory.md).

---

## Handoffs and routing

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `AgentTool` (delegate to sub-agent) | `handoff(agent)` / `handoffs=[agent]` | `HandoffAgent`, `agent_tool(agent)` |
| `SequentialAgent`, `ParallelAgent`, `LoopAgent` | — | `SequentialAgent`, `ParallelAgent`, `LoopAgent` |
| — | Triage agent pattern | `RouterAgent` (description-driven routing) |

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

See [agents/routing.md](agents/routing.md) and [agents/composition.md](agents/composition.md).

---

## Deployment

Neither ADK nor OpenAI Agents SDK has a built-in deployment target — you wire the agent to a serving layer yourself.

apx-agent ships two targets out of the box:

| Target | Command | When to use |
|---|---|---|
| **Databricks Apps** | `apx deploy --target apps` | Web app with `/_apx/*` UI, end-user OAuth, per-user identity passthrough |
| **Mosaic AI Model Serving** | `apx deploy --target model-serving` | REST API endpoint, notebook / pipeline / CI invocation, MLflow-native |

Same `agent.py`, one flag changes the target.

See [deploy/apps-vs-model-serving.md](deploy/apps-vs-model-serving.md).

---

## Observability / tracing

| Google ADK | OpenAI Agents SDK | apx-agent |
|---|---|---|
| `adk web` traces view | Built-in tracing on by default; 25+ integrations | MLflow autolog; `/_apx/traces` dev UI |
| Structured span output | `trace_id`, `group_id` on `RunConfig` | `apx.*` span attributes on every trace |
| `adk eval` | — | `apx eval evalset.jsonl` with LLM-as-judge |

Tracing is on by default. Every run creates a span tree in your workspace's MLflow experiment: LLM call, tool call, SQL execution, response. Access it:

- **Dev UI:** `/_apx/traces` at `http://localhost:8000/_apx/traces`
- **CLI:** `apx trace --agent <name>` (recent traces filtered by `apx.*` attributes)
- **Export:** `apx export-traces --table <catalog.schema.table> --hours 24` (Delta table)
- **Workspace:** Machine Learning → Experiments → your agent's experiment

---

## Databricks-specific concepts (no ADK / OpenAI SDK equivalent)

| apx-agent concept | What it does |
|---|---|
| `DataAgent("catalog", "schema")` | LlmAgent pre-grounded in a real UC schema; baked at scaffold time |
| `CoworkerAgent(...)` | DataAgent for two source systems joined on a shared business key |
| Identity passthrough (OBO) | App forwards caller's OAuth token; UC enforces per-user grants |
| `apx-agent scaffold` | Queries UC Tables API, writes `.apx/schema.json`, generates project layout |
| `apx-agent doctor` | Checks Python, uv, Databricks CLI, auth, and project layout; prints `Fix:` lines |
| `/_apx/topology` | Interactive graph of agents, tools, sub-agents, and platform resources |
| `GuardrailsConfig` | Declarative tool allowlist, rate limits, prompt injection detection |
| `WatchdogGuard` | Bridge to databricks-watchdog for cross-domain policy enforcement |

---

## Full parameter mapping

| ADK / OpenAI param | apx-agent param | Notes |
|---|---|---|
| `instruction` (ADK) | `instructions` | Both spellings accepted |
| `model` | `model` | Must be a Databricks serving endpoint name |
| `tools` | `tools` | `@tool` functions, governed primitives, or `agent_tool(agent)` wrappers |
| `before_tool_callback` (ADK) | `before_tool_callback` or `before_tool` | Both accepted; ADK form takes precedence |
| `after_tool_callback` (ADK) | `after_tool_callback` or `after_tool` | Both accepted |
| `before_model_callback` (ADK) | `before_model_callback` or `before_model` | Both accepted |
| `input_guardrails` (OpenAI) | `input_guardrails` | Same parameter name |
| `output_guardrails` (OpenAI) | `output_guardrails` | Same parameter name |
| `handoffs` (OpenAI) | `agents` on `HandoffAgent` | |
| `max_turns` (OpenAI) | `max_iterations` | Default 10 |
| `description` | `description` | Used in routing context and A2A discovery |
