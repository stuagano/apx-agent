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

> Python only. The TypeScript plugin has no `[[tool.apx.tools]]` equivalent — declare tools in code via the `tools` option.

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

## Declarative guardrails — `[tool.apx.agent.guardrails]`

> Python only. Guardrails declared here are additive over code-defined guards — code hooks run first, then config gates. All four built-in guard types are data-configurable.

```toml
[tool.apx.agent]
name = "customer_triage"
model = "databricks-claude-sonnet-4-6"

[tool.apx.agent.guardrails]
blocked_tools       = ["delete_account", "issue_refund"]
allowed_tools       = ["classify_intent", "get_recent_orders"]
rate_limit          = 60          # calls/min (global bucket)
rate_limit_burst    = 10          # burst cap; defaults to rate_limit
injection_detection = true        # prompt_injection_heuristic()
```

| Key | Type | Default | Effect |
|---|---|---|---|
| `blocked_tools` | `list[str]` | `[]` | `ToolDenylist` on `before_tool`; listed tools raise `PermissionError` at call time |
| `allowed_tools` | `list[str]` or absent | absent | `ToolAllowlist` on `before_tool`; only listed tools are permitted (an empty list blocks all) |
| `rate_limit` | `int` (calls/min) | absent | `RateLimit` on `before_tool`; single global bucket |
| `rate_limit_burst` | `int` | `rate_limit` | Burst cap for `rate_limit`; no effect when `rate_limit` is absent |
| `injection_detection` | `bool` | `false` | Appends `prompt_injection_heuristic()` to `input_guardrails`; scans message text for common injection patterns |

**Guard order within `before_tool`** (first raise wins): denylist → allowlist → rate limit. A denied call does not consume a rate-limit token. Code-defined `before_tool` always runs before config gates.

**Error handling:** A typo'd key (e.g. `rate_limt = 60`) is a hard validation error at startup — `GuardrailsConfig` uses `extra="forbid"`. A silent misconfiguration of a guard is worse than failing fast.

**Not config-expressible (code only):** `FeatureFlagGuard`, per-user rate limiting (`principal_key`), custom injection patterns (`patterns`), `WatchdogGuard`.

## Template-as-config — `template = { name = "...", ... }`

> Python only. Declare the entire agent as data — no `agent.py` required.

```toml
[tool.apx.agent]
name = "sales-coworker"
model = "databricks-claude-sonnet-4-6"
instructions = "Be concise and warm."        # persona overlay (E1)
template = { name = "data", catalog = "main", schema = "sales" }
```

The `template` inline-table selects a registered template by `name` and passes the remaining keys as the spec. The template wires the governed tools and sets grounded instructions (the **role**). The `[tool.apx.agent]` envelope (`model`, `instructions`, generation knobs) is the **persona** — layered on top afterward via the existing `compose_instructions` seam.

| Key | Purpose |
|---|---|
| `name` | Template registry key (e.g. `"data"`) |
| other keys | Template `Spec` fields (validated by the template's Pydantic `Spec`) |

**Built-in templates:**

| Name | Class | Spec fields |
|---|---|---|
| `data` | `DataTemplate` | `catalog`, `schema`, `warehouse_id?`, `genie_space?`, `vector_index?`, `include_functions?` |

**Interaction with `[[tool.apx.tools]]`:** Config-declared tools are additive — they attach after the template builds the leaf agent. A template-built `DataAgent` gets its wired SQL/Genie/UC tools from the template build; `[[tool.apx.tools]]` entries add on top. Code-wired tools win on name collision.

**Interaction with `[tool.apx.agent.guardrails]`** (E3c): the full finalize order is resolve (build-from-template) → apply_config_knobs (persona compose) → merge_config_tools → apply_config_guardrails.

**No `agent.py` required:** With a `template` configured, all runtimes (`apx run`, `apx deploy`, `apx info`, `apx eval`) build the agent from TOML alone. The `module` key is optional when `template` is set.

**Cross-repo templates:** Third-party templates register via the `apx_agent.templates` Python entry-point group — they appear in the registry after `pip install`. See the E1 spec and the `Template` protocol for authoring a template.
