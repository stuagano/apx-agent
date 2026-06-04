# DataAgent

A governed agent over a Unity Catalog schema. One line to create; automatically discovers tables, columns, and UC functions; runs SQL as the calling user.

```python
from apx_agent import DataAgent

agent = DataAgent("main", "sales")
```

Pass `ws=WorkspaceClient()` to ground it in the real schema at startup:

```python
from databricks.sdk import WorkspaceClient
agent = DataAgent("main", "sales", ws=WorkspaceClient())
```

## Constructor args

| Arg | Type | Default | What it does |
|---|---|---|---|
| `catalog` | `str` | required | UC catalog |
| `schema` | `str` | required | UC schema |
| `warehouse_id` | `str \| None` | `None` | SQL warehouse; falls back to env `WAREHOUSE_ID` |
| `ws` | `WorkspaceClient \| None` | `None` | Live workspace for schema introspection |
| `include_functions` | `bool` | `True` | Pull UC functions in `catalog.schema` as tools |
| `genie_space` | `str \| None` | `None` | Add a Genie space as a tool |
| `vector_index` | `str \| None` | `None` | Add a Vector Search index as a tool |
| `instructions` | `str \| None` | `None` | Override the auto-generated system prompt |
| `persona` | `str \| None` | `None` | Role phrase woven into the grounded instructions |
| `tables` | `dict \| None` | `None` | Explicit table metadata (skips live introspection) |
| `extra_tools` | `list \| None` | `None` | Additional `@tool` functions to register |
| `memory` | `str` | `"off"` | Memory tier — see below |
| `name` | `str \| None` | `"{schema}_data_agent"` | Agent name for traces |
| `**kwargs` | | | Passed to `LlmAgent` (temperature, max_tokens, etc.) |

## Schema grounding resolution order

At construction, `DataAgent` resolves the schema description used to generate instructions and configure the SQL tool:

1. **Explicit `tables=`** — caller-supplied metadata wins.
2. **Live introspection via `ws=`** — `SHOW TABLES` + column inspection if `ws` is provided.
3. **Baked `.apx/schema.json`** — written by `apx scaffold`; no live call needed at startup.
4. **Ungrounded fallback** — instructions omit table details; agent can still run SQL but won't name tables.

Scaffolded projects include `.apx/schema.json` so the agent starts grounded even with `ws=None`. Pass `ws=WorkspaceClient()` to pick up schema changes without re-scaffolding.

## Memory knob

Any agent — `DataAgent`, plain `Agent`, or `CoworkerAgent` — can remember across sessions using the `memory=` knob. It's a base-class feature on `LlmAgent`.

| `memory=` | Backend | Default for |
|---|---|---|
| `"off"` | none | `DataAgent`, `Agent` |
| `"inmemory"` | in-process | dev/testing |
| `"persistent"` / `"delta"` | UC Delta | `CoworkerAgent` |

```python
# DataAgent that also remembers
agent = DataAgent("main", "sales", memory="persistent")
```

For lakebase (pgvector), use explicit `[tool.apx.agent.memory]` TOML blocks.

## Identity passthrough

SQL runs as the **calling user**, not a service principal. Each request carries an OBO (on-behalf-of) token from the Databricks App's OAuth proxy. UC grants are enforced per-request — if the user can't `SELECT` a table, the agent can't either.

This is automatic. No configuration needed.

## Extending

`DataAgent` is a `LlmAgent`. You can add extra tools, compose it into a `SequentialAgent`, or subclass it:

```python
from apx_agent import DataAgent, tool

@tool
def send_alert(message: str) -> str:
    """Send an ops alert to Slack."""
    ...

agent = DataAgent(
    "main", "ops",
    extra_tools=[send_alert],
    persona="an ops engineer",
)
```

For an agent that also remembers, use `CoworkerAgent` — see [`coworker.md`](coworker.md).

## Further reading

| Goal | Doc |
|---|---|
| Agent with memory | [`coworker.md`](coworker.md) |
| Memory/session backends | [`sessions-and-memory.md`](sessions-and-memory.md) |
| UC function tools | [`governed-primitives.md`](governed-primitives.md) |
| pyproject.toml envelope | [`pyproject-toml.md`](pyproject-toml.md) |
