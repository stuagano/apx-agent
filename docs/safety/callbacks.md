# Callbacks

Four lifecycle hooks per `LlmAgent` — useful for cost tracking, prompt-injection scanning, output filtering, custom tracing, and approval gates.

```python
from apx_agent import Agent

def log_tool(name: str, args: dict) -> None:
    print(f"calling {name}({args})")

def reject_if_pii(prompts) -> None:
    text = str(prompts).lower()
    if "ssn" in text:
        raise PermissionError("PII guardrail: SSN detected")

agent = Agent(
    instructions="...",
    tools=[...],
    before_tool=log_tool,
    before_model=reject_if_pii,
    # after_tool, after_model also supported
)
```

| Hook | ADK alias | Signature | Fires |
|------|-----------|-----------|-------|
| `before_tool` | `before_tool_callback` | `(tool_name, arguments) -> None` | Before each tool dispatch — raise to abort |
| `after_tool`  | `after_tool_callback` | `(tool_name, arguments, output) -> None` | After each tool returns — raise propagates |
| `before_model` | `before_model_callback` | `(prompts) -> None` | Before each LLM invocation — raise to abort |
| `after_model` | `after_model_callback` | `(response) -> None` | After each LLM response — raise propagates |

Both the legacy names (`before_tool`) and the ADK-compatible names (`before_tool_callback`) are accepted. The ADK form takes precedence when both are supplied.

Sync and async hooks are both accepted. The wiring sits on top of LangChain's callback system, so anything the chain runtime can observe (LLM start/end, tool start/end) is reachable.

## Agent-level callbacks

Two agent-level callbacks fire around the full agent turn (not per-tool or per-model call):

| Callback | Signature | Fires |
|----------|-----------|-------|
| `before_agent_callback` | `(messages: list[Message]) -> None` | Before the agent processes a request — raise to block |
| `after_agent_callback` | `(text: str) -> None` | After the agent produces its final response |

```python
agent = Agent(
    instructions="...",
    before_agent_callback=lambda msgs: print(f"Starting turn: {len(msgs)} messages"),
    after_agent_callback=lambda text: print(f"Response length: {len(text)}"),
)
```
