# Cost tracking + callbacks

## Cost tracking

```python
from apx_agent import cost_for_agent
from databricks.sdk import WorkspaceClient

breakdown = cost_for_agent(
    agent_name="customer_triage",
    ws=WorkspaceClient(),
    lookback_hours=24,
    warehouse_id="wh-prod",
)
print(f"Total DBUs: {breakdown.total_dbus}")
print(f"Total USD:  ${breakdown.total_usd}")
for row in breakdown.rows:
    print(f"  {row['sku_name']}: {row['dbus']} DBUs → ${row['usd']}")
```

Queries `system.billing.usage` joined to `system.billing.list_prices` (best-effort) scoped to the agent's serving endpoint. `total_usd` is `None` when pricing isn't joinable for every row — partial coverage would understate the bill, so it's better to surface the gap than print a misleading total.

Same surface from the CLI:

```bash
apx cost --agent customer_triage --hours 24
apx cost --endpoint my-endpoint --hours 168 --format json
```

Requires the `system.billing` share to be enabled in the workspace.

## Callbacks

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

| Hook | Signature | Fires |
|------|-----------|-------|
| `before_tool` | `(tool_name, arguments) -> None` | Before each tool dispatch — raise to abort |
| `after_tool`  | `(tool_name, arguments, output) -> None` | After each tool returns — raise propagates |
| `before_model` | `(prompts) -> None` | Before each LLM invocation — raise to abort |
| `after_model` | `(response) -> None` | After each LLM response — raise propagates |

Sync and async hooks are both accepted. The wiring sits on top of LangChain's callback system, so anything the chain runtime can observe (LLM start/end, tool start/end) is reachable.
