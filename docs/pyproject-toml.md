# pyproject.toml reference

`pyproject.toml` is the **ops envelope** for an apx-agent project. It declares the model, agent name, and which external resources (memory, tools) the agent needs — without touching `agent.py`.

> **TOML is ops. Python is behavior.**
> Instructions, tool logic, and agent composition belong in `agent.py`. Model, resource names, and deployment knobs belong in `pyproject.toml`.

## Quick reference

```toml
[tool.apx.agent]
name        = "my-agent"
model       = "databricks-claude-sonnet-4-6"
description = "One-line description shown in the dev UI."
instructions = ""          # system prompt override (prefer agent.py)
temperature  = 0.0
max_tokens   = 4096
max_iterations = 10

# Starter prompts shown on the chat landing page
examples = [
    "What tables can you query?",
    "Show me the last 10 orders",
]

# Remote sub-agent URLs (A2A composition)
sub_agents = ["$CLASSIFIER_URL"]

# Declarative tools (alternative to code)
[[tool.apx.tools]]
type     = "genie"
space_id = "abc123"

[[tool.apx.tools]]
type       = "vector_search"
index_name = "main.search.docs_index"

# Memory backend (UC Delta — no extra infra)
[tool.apx.agent.memory]
type       = "delta"
table_name = "main.myapp.apx_memories"

# Session backend
[tool.apx.agent.session]
type       = "delta"
table_name = "main.myapp.apx_sessions"

# Input/output guards
[tool.apx.agent.guardrails]
pii_detection      = true
injection_detection = true
```

## Fields

### `[tool.apx.agent]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `name` | string | required | Agent name; used in traces and registry |
| `model` | string | `"databricks-meta-llama-3-3-70b-instruct"` | Model serving endpoint name |
| `description` | string | `""` | Dev UI metadata |
| `instructions` | string | `""` | System prompt; agent.py `instructions=` takes precedence |
| `temperature` | float | `null` (model default) | |
| `max_tokens` | int | `null` (model default) | |
| `max_iterations` | int | `10` | Tool-call loop safety cap |
| `examples` | list[str] | `[]` | Starter prompts on the chat landing page |
| `sub_agents` | list[str] | `[]` | Remote agent URLs (supports `$ENV_VAR`) |

### `[[tool.apx.tools]]`

Declarative alternative to code-wired tools. Each entry has a `type` key that names the factory:

| `type` | Factory | Required fields |
|---|---|---|
| `"genie"` | `genie_tool` | `space_id` |
| `"vector_search"` | `vector_search_tool` | `index_name` |
| `"sql"` | `sql_tool` | `warehouse_id` |
| `"uc_function"` | `uc_function_tool` | `name` |
| `"foundation_model"` | `foundation_model_tool` | `endpoint` |

Use code when the tool needs custom logic; use config for plain resource references.

### `[tool.apx.agent.memory]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `type` | `"inmemory" \| "delta" \| "lakebase"` | `"inmemory"` | |
| `table_name` | string | required for delta | Three-part UC name |
| `embedding_model` | string | `null` | Required for semantic search |
| `embedding_dim` | int | `null` | Required when `embedding_model` set |
| `auto_create` | bool | `true` | Create Delta table on first use |
| `namespace_default` | string | `"default"` | Per-agent memory partition |

### `[tool.apx.agent.session]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `type` | `"inmemory" \| "delta" \| "lakebase"` | `"inmemory"` | |
| `table_name` | string | required for delta | Three-part UC name |
| `auto_create` | bool | `true` | |
| `warehouse_id` | string | `null` | SQL warehouse for Delta reads |

### `[tool.apx.agent.guardrails]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `pii_detection` | bool | `false` | Redact PII in responses |
| `injection_detection` | bool | `false` | Flag prompt injection attempts |

## What goes where

| | `agent.py` | `pyproject.toml` |
|---|---|---|
| Instructions / system prompt | ✓ (preferred) | fallback |
| Tool logic | ✓ | |
| Tool resource names (Genie space, etc.) | ✓ or | ✓ (`[[tool.apx.tools]]`) |
| Model name | | ✓ |
| Memory/session backend | | ✓ (or `memory=` knob in agent.py) |
| Agent name | | ✓ |
| Starter prompts (`examples`) | | ✓ |
| Sub-agent URLs | | ✓ |
| Guards | | ✓ |

## Annotated payroll coworker example

```toml
[tool.apx.agent]
name        = "payroll-coworker"
model       = "databricks-claude-sonnet-4-6"
description = "Answers payroll questions across Kronos and Workday."

# No instructions here — agent.py CoworkerAgent builds them from the schema.

[tool.apx.agent.memory]
type       = "delta"
table_name = "main.payroll.apx_memories"   # auto-created

[tool.apx.agent.session]
type       = "delta"
table_name = "main.payroll.apx_sessions"   # auto-created
```

The matching `agent.py`:

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    # memory= not needed here — explicit TOML blocks above take precedence
)
```

The TOML blocks override the `memory=` knob when both are present, so you can add specific table names or embedding config without changing `agent.py`.

## Further reading

For the full configuration reference including lakebase, embedding options, and advanced guardrail configuration, see [`configuration.md`](configuration.md).
