# Configuration

**Python** — `[tool.apx.agent]` in `pyproject.toml`:

```toml
[tool.apx.agent]
name = "data_triage"
description = "Investigate missing data"
model = "databricks-claude-sonnet-4-6"
instructions = "System prompt for the agent"
max_iterations = 10
sub_agents = ["endpoints/sql-explainer"]   # Model Serving target
# sub_agents = ["$SQL_EXPLAINER_URL"]      # Apps target
```

**TypeScript** — plugin options:

```typescript
createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: 'System prompt for the agent',
  tools: [myTool],
  subAgents: ['endpoints/sql-explainer'],
})
```

Environment variable references (`$VAR` or `${VAR}`) are resolved at startup.

## Declarative tools — `[[tool.apx.tools]]`

Resource-reference tools can be declared as data instead of code. Each `[[tool.apx.tools]]` entry is a table-array sibling of `[tool.apx.agent]`; `type` selects a [platform tool factory](governed-primitives.md#platform-tool-factories) and the remaining keys are its arguments:

```toml
[tool.apx.agent]
name = "data_triage"
model = "databricks-claude-sonnet-4-6"

[[tool.apx.tools]]
type = "genie"
space_id = "$ACCOUNT_GENIE_SPACE_ID"   # $VAR / ${VAR} resolved at load time
name = "ask_account_data"
description = "Ask a question about a customer's account."

[[tool.apx.tools]]
type = "vector_search"
index_name = "main.search.docs_index"
columns = ["doc_id", "title", "content"]
num_results = 5

[[tool.apx.tools]]
type = "sql"
warehouse_id = "$SQL_WAREHOUSE_ID"
```

`type` accepts any platform factory: `genie`, `genie_query`, `vector_search`, `uc_function`, `uc_function_toolkit`, `catalog`, `schema`, `lineage`, `sql`, `http`, `openapi`, `mcp_tool`, `mcp_toolkit`, `foundation_model`, `jobs`, `jobs_for_table`, `jobs_history`, `jobs_logs`, `jobs_source_paths`. (`uc_function_toolkit`, `jobs`, and `mcp_toolkit` each return several tools.)

Config tools are **additive** and are merged onto the agent on every runtime — serve, deploy/log, model-serving predict, and `apx info` / `lint` / `eval`. Their resource grants (Genie space, warehouse, …) are auto-declared at log time, exactly like code-wired tools. A code-wired tool with the same `name` wins (the config entry is ignored, with a warning), so config is purely additive over code.

**Trust & failure controls (environment variables):**

- `APX_TOOLS_ALLOWED_HOSTS` — comma-separated host allow-list. When set, `openapi` / `mcp_tool` / `mcp_toolkit` tools may only point at those hosts; an out-of-list host is a hard error. Unset (the default) means no restriction.
- `APX_TOOLS_STRICT=1` — promote a tool whose factory fails at load time (e.g. an unreachable MCP server) to a hard error. The default is to skip that tool with a warning so one bad endpoint doesn't take the whole agent down.
