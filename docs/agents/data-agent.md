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
3. **`.apx/schema.json`** — the baked schema manifest written when a project
   is generated, either immediately by `apx-agent agents scaffold`/
   `apx-agent generate`, or during `apx-agent agents deploy <spec>.yaml` for
   a hand-authored spec; survives deploy without a `ws` arg
4. **Ungrounded fallback** — generic data-assistant instructions; still
   functional, just not schema-aware

### Grounding asset lifecycle

Scaffolding is not a one-time snapshot. A full project scaffold keeps its
grounding under `.apx/` so the project can be refreshed in place:

| Stage | Command or file | What is authoritative |
|---|---|---|
| Create | `apx-agent agents scaffold <name> --catalog <catalog> --schema <schema>` | Generates `.apx/schema.json`; newer projects also generate `.apx/okf/`. |
| Refresh | `apx-agent agents refresh-schema` | Re-introspects the configured UC schema and updates table/column metadata without regenerating the project. |
| Enrich | Edit `.apx/okf/tables/*.md`, or use `apx-agent agents pull-comments` | `.apx/okf/` is the source of truth for enriched grounding; curated descriptions are preserved by refresh. |
| Migrate | `apx-agent agents migrate-to-okf` | Converts a legacy `.apx/schema.json` project to `.apx/okf/` and regenerates `schema.json` as a derived cache. |

Run `refresh-schema` from inside the generated project. It preserves local-only
tables and enriched OKF sections by default, so adding a table in Unity Catalog
does not require scaffolding again. Use
`--prune-missing-tables` only when you intentionally want concepts for tables
removed from the live schema deleted; this is destructive to local-only and
hand-authored table concepts.

YAML is the source of truth, but `agents run <spec>.yaml` and
`agents deploy <spec>.yaml` now materialize a sibling project directory (for
example `sales.yaml` → `sales/`) and refresh its generated files on each run.
The retained `.apx/` assets make grounding available across restarts. Run
`apx-agent agents refresh-schema` (or `migrate-to-okf`) inside that materialized
directory for day-two refreshes and enrichment; edit the YAML for agent
configuration changes. After changing a grounding bundle, restart the local
agent or redeploy so the generated files are loaded by the runtime.

For most production deployments (Databricks Apps): `apx-agent agents scaffold`
generates a full project directory (`agent.py` + `pyproject.toml` +
`databricks.yml` + the baked `.apx/schema.json`), and `apx-agent agents deploy`
consumes that generated project directory. The framework loads the baked
manifest at startup, so no `ws` is needed at construction.

### Identity passthrough

The SQL tool runs queries as the **calling user**, not the app's service
principal. Their UC grants apply at query time. The agent can't touch what
they can't touch. No auth code at the tool level — the framework handles it.

### Robustness & day-two ops

Construction never raises. Schema introspection is best-effort — a permission,
network, or missing-warehouse failure degrades to the ungrounded fallback (a
working generic SQL assistant) rather than crashing the agent or the deploy.

Crucially, the agent doesn't just log the degradation — it **captures it** as
structured state you can read on day two:

- `agent._apx_data` is a `DataAgentHealth` (exported from `apx_agent`):
  `grounded`, `schema_source` (`tables` / `introspect` / `knowledge` / `baked` /
  `ungrounded`), `table_count`, `uc_functions` (`wired` / `deferred` /
  `disabled` / `none`), and `warehouse` (`declared` / `auto` / `unavailable`).
- `agent._apx_data_degraded` is a short reason string (or `None` when healthy);
  an unavailable warehouse outranks ungrounded since it breaks every query.

The deployed runtime surfaces this at **`GET /readyz`** under `checks.data`,
alongside `llm`, `tracing`, and `memory`. It is **informational** — an
ungrounded agent is still a working assistant, so `data` never flips readiness
to `degraded` (unlike `memory`). Operators poll `/readyz` to spot grounding loss
or a vanished warehouse after a redeploy or upstream schema change.

UC functions discovered later (the import-time `ws=None` path) are wired by the
idempotent `bind_workspace(ws)`, which also refreshes `_apx_data` so the
day-two state stays accurate. See the
[DataAgent tool discovery and wiring loop](../loops/README.md) for the
discover → wire → read-back cycle this state supports.

#### Active probe — `agent.probe(ws)`

`/readyz` reports health captured at construction; it does not re-read the
workspace on every request. For an **active** day-two check — *is the warehouse
reachable, and does the schema still match what we grounded on right now?* —
call `probe(ws)` off the hot path (a scheduled job, a notebook, an ops CLI):

```python
p = agent.probe(ws)                 # best-effort; never raises
if not p.ok:
    alert(p.detail)                 # e.g. "schema drift: missing ['customers']; new ['payments']"
# p.warehouse: "reachable" | "unavailable"
# p.schema:    "match" | "drift" | "unreachable" | "ungrounded"
# p.missing_tables / p.new_tables:  the exact drift
```

It re-introspects the live schema and diffs it against the agent's own
`uc_table` resources (no extra state to drift), and resolves the warehouse to
confirm it's reachable. Returns a `DataAgentProbe`. Run it on a cadence to catch
schema drift or a vanished warehouse before users hit a failed query — the
"trace regression triage" and discovery-and-wiring loops in
[docs/loops](../loops/README.md) both build on this signal.

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
