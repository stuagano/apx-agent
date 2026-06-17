# DataAgent

A **DataAgent** is an `LlmAgent` wired to a Unity Catalog schema in one line.
It discovers the tables, grounds its instructions in the real columns, wires a
SQL tool that runs as the calling user (UC grants enforced per-request), and
optionally wires UC functions, Genie, and Vector Search from the same schema.

---

## The one-liner

```python
from apx_agent import DataAgent

agent = DataAgent("main", "sales")
```

That's a working agent. Two required args — catalog and schema — and
everything else has a sensible default.

---

## All arguments

```python
agent = DataAgent(
    "main",               # catalog
    "sales",              # schema
    warehouse_id="abc",   # SQL warehouse; auto-discovered if omitted
    ws=WorkspaceClient(), # introspects schema at construction; optional
    persona="a sales analyst",   # role string woven into grounded instructions
    genie_space="abc123", # adds a genie_tool; optional
    vector_index="main.sales.embeddings",  # adds vector_search_tool; optional
    include_functions=True,  # wire UC functions from catalog.schema (needs ws)
    tables={"orders": ["id(bigint)", "amount(decimal)"]},  # pre-baked schema
    instructions="...",   # override the auto-generated grounding entirely
    name="sales-agent",   # defaults to "{schema}_data_agent"
    extra_tools=[my_tool],  # append additional tools
)
```

### How schema grounding resolves

The agent grounds its instructions in the actual table columns. Resolution
order (first match wins):

1. **`tables=`** explicit override — use this in tests or when you have the
   schema from another source
2. **`ws=` live introspection** — discovers tables and columns from the
   workspace at construction time
3. **`.apx/schema.json`** — the baked schema manifest written by `apx-agent agents scaffold`
   (same catalog+schema); survives deploy without a `ws` arg
4. **Ungrounded fallback** — generic data-assistant instructions; still
   functional, just not schema-aware

For most production deployments (Databricks Apps), use the baked manifest
path: `apx-agent agents scaffold` writes `.apx/schema.json`, the framework loads it at
startup, no `ws` needed at construction.

### Identity passthrough

The SQL tool runs queries as the **calling user**, not the app's service
principal. Their UC grants apply at query time. The agent can't touch what
they can't touch. No auth code at the tool level — the framework handles it.

---

## Extending a DataAgent

Add tools on top of the base schema wiring:

```python
from apx_agent import DataAgent, uc_function_tool

agent = DataAgent(
    "main", "sales",
    genie_space="abc123",       # Genie for natural-language data queries
    vector_index="main.sales.product_docs",  # semantic search
    extra_tools=[uc_function_tool("main.tools.send_alert")],
)
```

Or compose it as a sub-agent inside a router:

```python
from apx_agent import RouterAgent, DataAgent

agent = RouterAgent([
    DataAgent("main", "sales",   name="sales"),
    DataAgent("main", "support", name="support"),
])
```

---

## CoworkerAgent

`CoworkerAgent` is a `DataAgent` subclass that adds `persona`, `join_key`,
and `objective` — the three identity knobs for a two-system join — plus a
`memory` knob for persistence across sessions. See
[`docs/agents/coworker.md`](coworker.md).

---

## Further reading

- [`docs/agents/coworker.md`](coworker.md) — `DataAgent + persona + join_key + objective + memory`
- [`docs/reference/configuration.md`](../reference/configuration.md) — full `[tool.apx.agent]` TOML reference
- [`docs/tools/overview.md`](../tools/overview.md) — `sql_tool`, `genie_tool`, `vector_search_tool`, `uc_function_tool`
- [`python/src/apx_agent/data_agent.py`](../../python/src/apx_agent/data_agent.py) — the implementation
