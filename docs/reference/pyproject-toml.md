# pyproject.toml and agent.py — where settings live

Service Policy declarations are documented in [service-policies.md](service-policies.md).

An apx-agent app splits its configuration across two places. The agent itself,
including its model, instructions, guardrails, memory and session backends,
lives in `agent.py`: the agent is always the variable named `agent`. Deploy and
dev-loop settings live in `pyproject.toml`, under `[tool.apx.deploy]` and
`[tool.apx]`.

> This is the quick reference. For exhaustive field documentation see
> [`docs/reference/configuration.md`](configuration.md).

---

## Minimal example

```python
# agent.py
from apx_agent import DataAgent

agent = DataAgent(
    "main", "sales",
    name="my-agent",
    description="What this agent does.",
    model="databricks-claude-sonnet-4-6",
)
```

```toml
# pyproject.toml
[tool.apx.deploy]
name = "my-agent"
```

---

## Agent fields (in `agent.py`)

| Field | Type | Default | What it does |
|---|---|---|---|
| `name` | string | required | Agent name; used in traces, the dev UI, and MLflow (deploy name: `[tool.apx.deploy]`) |
| `description` | string | `""` | Shown in the dev UI and `/.well-known/agent.json` |
| `model` | string | `"databricks-meta-llama-3-3-70b-instruct"` | Model serving endpoint name |
| `instructions` | string | `""` | System prompt prepended to every conversation |
| `temperature` | float | model default | Generation temperature |
| `max_tokens` | int | model default | Max output tokens |
| `max_iterations` | int | `10` | Safety cap on the tool-calling loop |
| `examples` | list[string] | `[]` | Starter prompts shown as chips on the chat landing page |
| `sub_agents` | list[string] | `[]` | URLs of remote agents to attach as tools |

### Declaring runtime settings

`instructions`, `temperature`, `max_tokens`, `max_iterations`, `sub_agents`,
`memory` and `session` are agent constructor arguments. The runtime settings
(`model`, `identity`, `target`, `recovery`, `gateway`, `service_policies`,
`guardrails`, `knowledge`, `vector_search_index`, `bindings`, `url`, `registry`,
`api_prefix`, `examples`, `workflows`, `data`, `executor`, `example`,
`description`) are keyword arguments on the root agent, or
`agent.configure(...)` for an agent built by a factory.

```python
agent = SequentialAgent(
    [researcher, Agent(name="writer", tools=[draft], model="system.ai.claude-opus-4-7")],
    model="system.ai.claude-sonnet-4-6",
    target="durable_agent_server",
)
```

Explicit memory and session backends go on the agent too:
`memory=MemoryBackendConfig(...)` and `session=SessionBackendConfig(...)`, or the
same as a dict.

`model` and `identity` are per-agent: an agent uses its own value, else its
nearest ancestor's, else the app's. Every other field is app-wide: set it on
the root; a different value on a child fails.

### `[tool.apx.deploy]` — deploy settings

Deploy settings live in their own table, which deploy reads without importing
`agent.py`:

```toml
[tool.apx.deploy]
name = "orders-agent"     # optional; defaults to [project].name
app_name = "mcp-orders"   # optional; Apps API App name, when it differs from name
host = "apps"             # apps
space = "shared-space"    # an App Space picks compute; without one:
# instances = 2           #   fixed count, or [tool.apx.deploy.autoscale] min/max, 1-5
backend = "agentbricks"   # | apps_api (+ entrypoint, include, env)
```

`[tool.apx.deploy]` also holds the registration and publishing keys:
`registered_model`, `catalog`, `schema`, `supervisor_id`, `registry_table` and
`tools_table`.

`agents deploy` records every App in the workspace agent registry. With no
`registry_table` / `tools_table` set, it lives in the workspace default catalog
(`<catalog>.apx.agent_registry` and `<catalog>.apx.agent_tools`) and is created by
the first deploy. Each deploy grants whatever is missing: `SELECT` for
`account users` and `SELECT, MODIFY` for its App's service principal, plus
`USE CATALOG` and `USE SCHEMA` on the parents for both. A deployer without
MANAGE gets the exact `GRANT` statements to hand to the owner. Pass
`--no-registry` to skip.

Dev-loop keys live in top-level `[tool.apx]`:

```toml
[tool.apx]
experiment = "/Users/you@example.com/orders"   # MLflow experiment for run/eval/trace
# profile lives in the gitignored .apx.local, under [tool.apx]
```

---

## Removed: `[tool.apx.agent]`

The `[tool.apx.agent]` table is no longer supported. To move an existing
project, commit your work and run `apx-agent agents migrate-config`. It moves
runtime settings onto the agent, deploy and registration keys to
`[tool.apx.deploy]`, and `profile` / `experiment` to `[tool.apx]`. It prints the
keyword arguments to add to your root agent; add them, commit, and run it again
to remove the old table. Nested tables map onto dicts: `guardrails` with
`injection_detection = true` becomes `guardrails={"injection_detection": True}`.

| Setting | Where it lives now |
|---|---|
| `model`, `identity`, `target`, `recovery`, `gateway`, `service_policies`, `guardrails`, `knowledge`, `vector_search_index`, `bindings`, `url`, `registry`, `api_prefix`, `examples`, `workflows`, `data`, `executor`, `example`, `description` | keyword argument on the root agent in `agent.py` (`Agent(..., guardrails={...})`), or `agent.configure(...)` for an agent built by a factory |
| `instructions`, `temperature`, `max_tokens`, `max_iterations`, `sub_agents`, `memory`, `session` | agent constructor arguments; `session=` takes a dict such as `{"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "x"}`; a Lakebase `memory=` dict also needs `embedding_model` and `embedding_dim` |
| `name`, `host`, deploy fields (space, instances, backend, entrypoint, include, autoscale, ...), `registered_model`, `catalog`, `schema`, `supervisor_id`, `registry_table`, `tools_table` | `[tool.apx.deploy]` in `pyproject.toml` |
| `profile`, `experiment`, `memory_store`, `example_store`, `summarize_fn` | `[tool.apx]` in `pyproject.toml` (`profile` usually in the gitignored `.apx.local`) |
| graph declarations (`agents` leaves + `root`), `template` | a YAML spec file (`my_agent.yaml`); `apx-agent agents run my_agent.yaml` materializes it |
| `module = "agent:agent"` | gone: the agent is always the `agent` variable in `agent.py` |

---

### `examples` — starter prompts

```python
agent = DataAgent(
    "main", "sales",
    examples=[
        "Show me the top accounts by revenue this quarter",
        "Which deals closed last month?",
    ],
)
```

Clickable chips on the chat landing page. UI-only — no effect on agent
behavior.

---

## Sub-sections

### `memory=` — facts memory

Persistent cross-session recall. Stores facts the agent should remember
across conversations.

```python
# Development: in-process, forgets on restart
memory={"type": "inmemory"}

# Production: pgvector on Lakebase (semantic similarity)
memory={
    "type": "lakebase",
    "host": "my-lakebase.db.databricks.com",
    "database": "agentdb",
    "embedding_model": "databricks-bge-large-en",
    "embedding_dim": 1024,
}

# Beta: workspace managed memory via AgentKit (requires apx-agent[agentbricks])
memory={"type": "managed", "store_name": "agent-memory"}
```

Memory tools scope operations to the trusted calling user. Managed-memory actor
IDs partition data; store permissions are the access-control boundary. Requests without a principal return `NO_PRINCIPAL`
without writing.

**A managed memory store is a shared, independently-owned resource — by design.**
One completed session is distilled only when `memory={"extraction": {...}}`
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
| `extraction` | — | Opt-in distillation of one completed managed session into this store. Absent means off. Requires `type = "managed"` and `session={"type": "managed"}`. |
| `extraction.enabled` | — | Must be true before `apx memory extract` calls the service. Defaults to false. |
| `extraction.instructions` | — | Approved distillation instructions sent with every extraction. |
| `auto_create` | no | Create table on first use (default `true`) |
| `namespace_default` | no | Default namespace (default `"default"`) |
| `tool_prefix` | no | Prefix for tool names (e.g. `"mem_"`) |
| `include` | no | Subset of tools: `["recall"]`, `["recall","remember"]` |

### `session=` — session continuity

Ties conversation turns together by `session_id`. History is loaded at the
start of each turn.

```python
# Development
session={"type": "inmemory"}

# Durable (survives restart), low-latency chat-style history
session={
    "type": "lakebase",
    "host": "my-lakebase.db.databricks.com",
    "database": "agentdb",
}
```

### `guardrails=` — built-in guards

```python
guardrails={
    "blocked_tools": ["dangerous_tool"],   # raises PermissionError at call time
    "allowed_tools": ["sql", "recall"],    # only these tools are permitted
    "rate_limit": 60,                      # calls/minute
    "rate_limit_burst": 10,
    "injection_detection": True,           # scan input for common injection patterns
}
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

```python
# agent.py
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    name="payroll-coworker",
    description="Payroll analyst across Kronos and Workday data.",
    model="databricks-claude-sonnet-4-6",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    max_iterations=15,
    examples=[
        "Why doesn't this paycheck match the hours worked?",
        "Show me all employees with a discrepancy this pay period",
        "What pay rules applied to employee 4821 last week?",
    ],
    memory={                              # persists across sessions
        "type": "lakebase",
        "host": "${LAKEBASE_HOST}",
        "database": "payroll_coworker",
        "embedding_model": "databricks-bge-large-en",
        "embedding_dim": 1024,
    },
    session={
        "type": "lakebase",
        "host": "${LAKEBASE_HOST}",
        "database": "payroll_coworker",
        "table_name": "main.payroll.apx_sessions",
    },
    guardrails={"rate_limit": 120},
)
```

```toml
# pyproject.toml
[tool.apx.deploy]
name = "payroll-coworker"
```

---

## What goes where

**`pyproject.toml` is ops. `agent.py` is the agent.**

| | `pyproject.toml` | `agent.py` |
|---|---|---|
| Deploy name, host, space, instances | `[tool.apx.deploy]` | — |
| MLflow experiment, profile | `[tool.apx]` | — |
| Model, description, instructions | — | agent arguments |
| Memory + session backend | — | `memory=`, `session=` |
| Rate limits, guardrails | — | `guardrails=` |
| UI starter prompts | — | `examples=` |
| What data it talks to | — | `DataAgent("catalog", "schema")` |
| What tools it has | — | `genie_space=`, `extra_tools=[...]` |
| Persona / role | — | `persona=` arg |
| How agents compose | — | `RouterAgent`, `SequentialAgent`, ... |

---

## Further reading

- [`docs/agents/data-agent.md`](../agents/data-agent.md) — DataAgent reference
- [`docs/agents/coworker.md`](../agents/coworker.md) — CoworkerAgent reference
- [`docs/reference/configuration.md`](configuration.md) — full field-by-field reference for every sub-section
