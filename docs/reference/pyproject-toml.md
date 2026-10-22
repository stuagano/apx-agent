# pyproject.toml — [tool.apx.agent]

Service Policy declarations are documented in [service-policies.md](service-policies.md).

Every apx-agent app has a `pyproject.toml` with a `[tool.apx.agent]` block.
This is the **app envelope**: it declares name, model, instructions, and where
to find the agent code. It is not the agent itself — that lives in `agent.py`.

> This is the quick reference. For exhaustive field documentation see
> [`docs/reference/configuration.md`](configuration.md).

---

## Minimal example

```toml
[tool.apx.agent]
name        = "my-agent"
description = "What this agent does."
model       = "databricks-claude-sonnet-4-6"
module      = "agent:agent"
```

`module = "agent:agent"` means: import the variable `agent` from the file
`agent.py`. That variable is your `DataAgent`, `CoworkerAgent`, or any other
`LlmAgent`.

---

## All top-level fields

| Field | Type | Default | What it does |
|---|---|---|---|
| `name` | string | required | Agent name; used in traces, the dev UI, and MLflow |
| `description` | string | `""` | Shown in the dev UI and `/.well-known/agent.json` |
| `model` | string | `"databricks-meta-llama-3-3-70b-instruct"` | Model serving endpoint name |
| `instructions` | string | `""` | System prompt prepended to every conversation |
| `module` | string | — | `"file:variable"` pointing at your agent (e.g. `"agent:agent"`) |
| `temperature` | float | model default | Generation temperature |
| `max_tokens` | int | model default | Max output tokens |
| `max_iterations` | int | `10` | Safety cap on the tool-calling loop |
| `examples` | list[string] | `[]` | Starter prompts shown as chips on the chat landing page |
| `sub_agents` | list[string] | `[]` | URLs of remote agents to attach as tools |

### Setting these in Python

Any agent accepts the runtime fields as keyword arguments. A field set both in
Python and in `[tool.apx.agent]` must have the same value.

```python
# before: pyproject.toml
# [tool.apx.agent]
# model = "system.ai.claude-sonnet-4-6"
# target = "durable_agent_server"

# after: agent.py
agent = SequentialAgent(
    [researcher, Agent(name="writer", tools=[draft], model="system.ai.claude-opus-4-7")],
    model="system.ai.claude-sonnet-4-6",
    target="durable_agent_server",
)
```

`model` and `identity` are per-agent: an agent uses its own value, else its
nearest ancestor's, else the app's. Every other field is app-wide: set it on
the root; a different value on a child fails.

### `examples` — starter prompts

```toml
examples = [
    "Show me the top accounts by revenue this quarter",
    "Which deals closed last month?",
]
```

Clickable chips on the chat landing page. UI-only — no effect on agent
behavior.

---

## Sub-sections

### `[tool.apx.agent.memory]` — facts memory

Persistent cross-session recall. Stores facts the agent should remember
across conversations.

```toml
# Development: in-process, forgets on restart
[tool.apx.agent.memory]
type = "inmemory"

# Production: pgvector on Lakebase (semantic similarity)
[tool.apx.agent.memory]
type           = "lakebase"
host           = "my-lakebase.db.databricks.com"
database       = "agentdb"
embedding_model = "databricks-bge-large-en"
embedding_dim  = 1024

# Beta: workspace managed memory via AgentKit (requires apx-agent[agentbricks])
[tool.apx.agent.memory]
type       = "managed"
store_name = "agent-memory"
```

Memory tools scope operations to the trusted calling user. Managed-memory actor
IDs partition data; store permissions are the access-control boundary. Requests without a principal return `NO_PRINCIPAL`
without writing.

**A managed memory store is a shared, independently-owned resource — by design.**
One completed session is distilled only when `[tool.apx.agent.memory.extraction]`
sets `enabled = true`. `apx memory extract --session-id <id> --actor-id <caller>`
sends that session to the bound memory store. `--dry-run` returns the entries
without writing them. The session is not deleted, and this does not consolidate
memories that already exist.

Unlike a session store (which belongs to one agent's conversations and is
provisioned when that agent deploys), a memory store is meant to outlive and be
**shared across multiple agents**: provision it once, then have each agent bind
the same `store_name` so a fleet accumulates and recalls common institutional
memory. That is why memory has its own standalone provisioning path —
`apx-agent memory provision --store <name>` (and `provision_managed_memory`,
create-only-on-404) — separate from any agent's deploy. Create the store first,
grant the agents' service principals access, then declare `store_name` in each.
This asymmetry with sessions is intentional, not an inconsistency: shared
lifetime (memory, provisioned independently) vs. per-agent lifetime (session,
provisioned at deploy).

| Key | Required | Description |
|---|---|---|
| `type` | yes | `"inmemory"` / `"lakebase"` / `"managed"` |
| `embedding_model` | lakebase | Databricks serving endpoint for embeddings |
| `embedding_dim` | lakebase | Embedding dimensionality |
| `database` | lakebase | Postgres database name |
| `host` | lakebase | Lakebase endpoint DNS; supports `$ENV_VAR` |
| `table_name` | lakebase | UC table path (`catalog.schema.table`) |
| `store_name` | managed | Workspace memory store display name |
| `extraction` | — | Opt-in distillation of one completed managed session into this store. Absent means off. Requires `type = "managed"` and `[tool.apx.agent.session] type = "managed"`. |
| `extraction.enabled` | — | Must be true before `apx memory extract` calls the service. Defaults to false. |
| `extraction.instructions` | — | Approved distillation instructions sent with every extraction. |
| `auto_create` | no | Create table on first use (default `true`) |
| `namespace_default` | no | Default namespace (default `"default"`) |
| `tool_prefix` | no | Prefix for tool names (e.g. `"mem_"`) |
| `include` | no | Subset of tools: `["recall"]`, `["recall","remember"]` |

### `[tool.apx.agent.session]` — session continuity

Ties conversation turns together by `session_id`. History is loaded at the
start of each turn.

```toml
# Development
[tool.apx.agent.session]
type = "inmemory"

# Durable (survives restart), low-latency chat-style history
[tool.apx.agent.session]
type          = "lakebase"
host          = "my-lakebase.db.databricks.com"
database      = "agentdb"
```

### `[tool.apx.agent.guardrails]` — built-in guards

```toml
[tool.apx.agent.guardrails]
blocked_tools        = ["dangerous_tool"]   # raises PermissionError at call time
allowed_tools        = ["sql", "recall"]    # only these tools are permitted
rate_limit           = 60                   # calls/minute
rate_limit_burst     = 10
injection_detection  = true                 # scan input for common injection patterns
```

Guard order within `before_tool` (first raise wins): denylist → allowlist →
rate limit.

### Tools belong in Python

Use existing factories in an ordinary `tools/` package:

```python
# tools/data.py
import os

from apx_agent import genie_tool, vector_search_tool

ask_data = genie_tool(
    os.environ["GENIE_SPACE_ID"],
    name="ask_data",
    description="Answer questions from a Genie space.",
)
search_docs = vector_search_tool("main.docs.embeddings", name="search_docs")
```

Import `ask_data` and `search_docs` explicitly in `agent.py` and include them in
`Agent(tools=[ask_data, search_docs])` or a data agent's `extra_tools`. Legacy
`[[tool.apx.tools]]` tables are rejected; remove them after migration. Non-tool
APX settings stay in `pyproject.toml`. `.build/agent.toml` remains generated
runtime output, not an authoring surface.

See [Python-native tool layout](../tools/overview.md#python-native-tool-layout)
for the directory structure and preservation of tool resources, identity, and
scope via `build_tool(scope=ToolScope(...))`.

---

## Complete annotated example

```toml
[tool.apx.agent]
name        = "payroll-coworker"
description = "Payroll analyst across Kronos and Workday data."
model       = "databricks-claude-sonnet-4-6"
module      = "agent:agent"           # imports `agent` from agent.py
instructions = ""                     # CoworkerAgent sets its own grounded instructions
max_iterations = 15

examples = [
    "Why doesn't this paycheck match the hours worked?",
    "Show me all employees with a discrepancy this pay period",
    "What pay rules applied to employee 4821 last week?",
]

[tool.apx.agent.memory]
type            = "lakebase"           # persists across sessions
host            = "${LAKEBASE_HOST}"
database        = "payroll_coworker"
embedding_model = "databricks-bge-large-en"
embedding_dim   = 1024

[tool.apx.agent.session]
type       = "lakebase"
host       = "${LAKEBASE_HOST}"
database   = "payroll_coworker"
table_name = "main.payroll.apx_sessions"

[tool.apx.agent.guardrails]
rate_limit = 120
```

And the matching `agent.py`:

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    memory="persistent",              # picks up [tool.apx.agent.memory] if present
)
```

---

## What goes where

**TOML is ops. Python is behavior.**

You could swap the model or tighten a rate limit without touching `agent.py`.
You could point the agent at a different schema without touching `pyproject.toml`.

| | `pyproject.toml` | `agent.py` |
|---|---|---|
| Name, model, description | ✓ | — |
| Memory + session backend | ✓ | — |
| Rate limits, guardrails | ✓ | — |
| UI starter prompts | ✓ | — |
| What data it talks to | — | `DataAgent("catalog", "schema")` |
| What tools it has | — | `genie_space=`, `extra_tools=[...]` |
| Persona / role | — | `persona=` arg |
| How agents compose | — | `RouterAgent`, `SequentialAgent`, … |

---

## Further reading

- [`docs/agents/data-agent.md`](../agents/data-agent.md) — DataAgent reference
- [`docs/agents/coworker.md`](../agents/coworker.md) — CoworkerAgent reference
- [`docs/reference/configuration.md`](configuration.md) — full field-by-field reference for every sub-section
