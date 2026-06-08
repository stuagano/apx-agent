# Callbacks and guardrails

Lifecycle hooks that fire around model calls, tool calls, and full agent turns. Use them for input screening, output filtering, PII redaction, cost tracking, approval gates, and custom tracing.

> **Coming from ADK?** These map directly: `before_tool` / `before_tool_callback` (both accepted) ≈ ADK `before_tool_callback`. `before_model` / `before_model_callback` ≈ ADK `before_model_callback`. Raise to abort — same pattern.
>
> **Coming from OpenAI Agents SDK?** `before_model` is equivalent to an input guardrail; `after_model` is equivalent to an output guardrail; `before_tool` is equivalent to a tool guardrail. The decorator-based guardrail pattern (`@input_guardrail`, `@output_guardrail`) is not used — raise from the callback directly to abort or filter.

---

## Hook reference

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

### Tool hooks

| Hook | ADK alias | Signature | Fires |
|------|-----------|-----------|-------|
| `before_tool` | `before_tool_callback` | `(tool_name: str, arguments: dict) -> None` | Before each tool dispatch — raise to abort |
| `after_tool`  | `after_tool_callback`  | `(tool_name: str, arguments: dict, output: Any) -> None` | After each tool returns — raise to propagate |

**Aborting a tool call:** Raise any exception from `before_tool` and the tool call is cancelled. The exception message surfaces as an error to the LLM, which can recover or report it.

```python
def enforce_tool_allowlist(name: str, args: dict) -> None:
    allowed = {"sql_query", "get_schema"}
    if name not in allowed:
        raise PermissionError(f"Tool '{name}' is not permitted in this agent.")
```

### Model hooks

| Hook | ADK alias | Signature | Fires |
|------|-----------|-----------|-------|
| `before_model` | `before_model_callback` | `(prompts: list) -> None` | Before each LLM invocation — raise to abort |
| `after_model`  | `after_model_callback`  | `(response: Any) -> None` | After each LLM response — raise to propagate |

**Input screening:** `before_model` receives the full prompt list (system + history + user message). Raise to block the call — equivalent to an input guardrail.

```python
BLOCKED_PHRASES = ["ignore previous instructions", "you are now"]

def injection_screen(prompts) -> None:
    text = " ".join(str(p) for p in prompts).lower()
    for phrase in BLOCKED_PHRASES:
        if phrase in text:
            raise PermissionError(f"Prompt injection detected: '{phrase}'")
```

**Output filtering:** `after_model` receives the raw model response. Raise to prevent the response from reaching the user.

```python
import re

def redact_pii(response) -> None:
    text = str(response)
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b", text):   # SSN pattern
        raise PermissionError("Response contains PII — blocked.")
```

### Agent-level hooks

Two hooks fire around the full agent turn — not per-tool or per-model call:

| Callback | Signature | Fires |
|----------|-----------|-------|
| `before_agent_callback` | `(messages: list[Message]) -> None` | Before the agent processes a request — raise to block the turn |
| `after_agent_callback`  | `(text: str) -> None` | After the agent produces its final response |

```python
agent = Agent(
    instructions="...",
    before_agent_callback=lambda msgs: print(f"Starting turn: {len(msgs)} messages"),
    after_agent_callback=lambda text: print(f"Response length: {len(text)}"),
)
```

Use `before_agent_callback` for rate limiting, auth checks, or logging turn metadata. Use `after_agent_callback` for response auditing or downstream notifications.

---

## Hook names

Both the short names (`before_tool`, `after_tool`, `before_model`, `after_model`) and the ADK-compatible long names (`before_tool_callback`, `after_tool_callback`, `before_model_callback`, `after_model_callback`) are accepted. The long form takes precedence when both are supplied.

```python
# All four forms are equivalent:
agent = Agent(instructions="...", before_tool=my_fn)
agent = Agent(instructions="...", before_tool_callback=my_fn)  # ADK-compatible name
```

Both sync and async hooks are accepted. Async hooks are awaited; sync hooks are called directly.

---

## Common patterns

### PII redaction on tool output

```python
import re

PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "cc":  re.compile(r"\b(?:\d{4}[- ]){3}\d{4}\b"),
}

def redact_tool_output(name: str, args: dict, output: Any) -> None:
    text = str(output)
    for label, pat in PATTERNS.items():
        if pat.search(text):
            raise PermissionError(f"Tool '{name}' returned {label.upper()} — blocked.")
```

### Cost / rate tracking

```python
from datetime import datetime, timedelta
from collections import deque

_window: deque = deque()

def rate_limit(name: str, args: dict) -> None:
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=1)
    while _window and _window[0] < cutoff:
        _window.popleft()
    if len(_window) >= 60:
        raise PermissionError("Rate limit exceeded (60 tool calls/min).")
    _window.append(now)
```

### Approval gate (async)

```python
import asyncio

async def human_approve(name: str, args: dict) -> None:
    """Block tool calls that write data until a human approves."""
    WRITE_TOOLS = {"insert_row", "update_record", "delete_record"}
    if name in WRITE_TOOLS:
        approved = await ask_human_for_approval(name, args)
        if not approved:
            raise PermissionError(f"Human rejected '{name}' call.")
```

---

## Wiring summary

```
Agent turn
│
├── before_agent_callback(messages)         ← raise to block entire turn
│
├── [LLM call loop]
│   ├── before_model_callback(prompts)      ← raise to abort LLM call
│   ├── [LLM responds]
│   └── after_model_callback(response)      ← raise to block response
│
│   ├── before_tool_callback(name, args)    ← raise to abort tool call
│   ├── [tool executes]
│   └── after_tool_callback(name, args, output) ← raise to block result
│
└── after_agent_callback(final_text)        ← raise to suppress response
```

For built-in guard configuration (allowed tools, rate limits, injection detection, audit logging), see [compliance.md](compliance.md). For identity-based authorization on each turn, see [identity-passthrough.md](identity-passthrough.md).
