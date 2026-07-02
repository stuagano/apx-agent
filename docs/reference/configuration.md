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

Resource-reference tools can be declared as data instead of code. Each `[[tool.apx.tools]]` entry is a table-array sibling of `[tool.apx.agent]`; `type` selects a [platform tool factory](../tools/overview.md#platform-tool-factories) and the remaining keys are its arguments:

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

`type` accepts any platform factory: `genie`, `genie_query`, `vector_search`, `uc_function`, `uc_function_toolkit`, `catalog`, `schema`, `lineage`, `sql`, `http`, `openapi`, `mcp_tool`, `mcp_toolkit`, `foundation_model`, `jobs`, `jobs_for_table`, `jobs_history`, `jobs_logs`, `jobs_source_paths`, `uc_comment_writer`. (`uc_function_toolkit`, `jobs`, and `mcp_toolkit` each return several tools.)

Config tools are **additive** and are merged onto the agent on every runtime — serve, deploy/log, model-serving predict, and `apx-agent agents describe` / `eval lint` / `eval`. Their resource grants (Genie space, warehouse, …) are auto-declared at log time, exactly like code-wired tools. A code-wired tool with the same `name` wins (the config entry is ignored, with a warning), so config is purely additive over code.

**Trust & failure controls (environment variables):**

- `APX_TOOLS_ALLOWED_HOSTS` — comma-separated host allow-list. When set, `openapi` / `mcp_tool` / `mcp_toolkit` tools may only point at those hosts; an out-of-list host is a hard error. Unset (the default) means no restriction.
- `APX_TOOLS_STRICT=1` — promote a tool whose factory fails at load time (e.g. an unreachable MCP server) to a hard error. The default is to skip that tool with a warning so one bad endpoint doesn't take the whole agent down.

### `type = "uc_comment_writer"` — governed UC COMMENT writes

Writes a Unity Catalog `COMMENT` on a table or column via the SQL warehouse, running as the **calling user** (OBO token). The calling user must hold the `MODIFY` privilege on the target table; the write is audited through Unity Catalog. Permission errors are returned to the agent as a structured error message — they are never silently swallowed.

**Opt-in only.** This tool is present only when declared via `[[tool.apx.tools]]`. It is never wired by default. Any config that omits a `uc_comment_writer` entry will not include it.

**Scoped.** The `catalog` and `schema` are fixed at declaration time. The agent supplies only `table` (within that schema), an optional `column`, and the `comment` text. Identifiers are validated against a safe-identifier pattern and the comment text is SQL-literal-escaped before execution.

```toml
[[tool.apx.tools]]
type = "uc_comment_writer"
catalog = "main"
schema = "sales"
warehouse_id = "abc123"
```

| Key | Required | Description |
|---|---|---|
| `catalog` | yes | UC catalog that scopes the writes |
| `schema` | yes | UC schema within `catalog` |
| `warehouse_id` | no | SQL warehouse to execute the `COMMENT` statement; auto-discovered at call time if omitted |
| `name` | no | Tool name exposed to the LLM (default: `"update_uc_comment"`) |
| `description` | no | Override the auto-generated tool description |

**OKF tie-in.** An OKF-grounded agent can use `uc_comment_writer` to push curated table and column descriptions back to Unity Catalog under full UC governance — closing the loop from read-time grounding to write-time curation.

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
| `coworker` | `CoworkerTemplate` | `catalog`, `schema`, `persona?`, `join_key?`, `objective?`, `memory?`, `warehouse_id?`, `genie_space?`, `vector_index?`, `include_functions?` |

**Interaction with `[[tool.apx.tools]]`:** Config-declared tools are additive — they attach after the template builds the leaf agent. A template-built `DataAgent` gets its wired SQL/Genie/UC tools from the template build; `[[tool.apx.tools]]` entries add on top. Code-wired tools win on name collision.

**Interaction with `[tool.apx.agent.guardrails]`** (E3c): the full finalize order is resolve (build-from-template) → apply_config_knobs (persona compose) → merge_config_tools → apply_config_guardrails.

**No `agent.py` required (for introspection and deploy):** With a `template` configured, the introspection and deploy runtimes — `apx-agent agents describe`, `apx-agent eval lint`, `apx-agent eval`, and `apx-agent agents deploy` — build the agent from TOML alone; the `module` key is optional for them. **`apx-agent agents run` is the exception:** it serves an ASGI app module (`app:app`), so a template-only project still needs a minimal `app.py` that calls `create_app()` without importing a code agent — e.g. `create_app(agent=None)`, which resolves the template at startup. Generating that `app.py` automatically for template-only projects is a planned scaffold follow-up.

**Precedence when both `template` and a code agent are present:** on the CLI and deploy paths (`apx-agent agents describe`, `apx-agent eval lint`, `apx-agent eval`, `apx-agent agents deploy`), `template` wins — `resolve_agent` checks `config.template` before falling through to the module import. On the serve path, an explicit `create_app(agent=...)` wins because `resolve_agent` is skipped entirely when a pre-built agent is supplied. The practical consequence: a project with both a `template` field and an `agent.py` will get the template agent from `apx-agent agents deploy`/`apx-agent agents describe` but the code agent from `apx-agent agents run` (if `app.py` imports it) — a silent divergence. For a clean setup, use *either* a `template` *or* a code `agent.py`, not both.

**Cross-repo templates:** Third-party templates register via the `apx_agent.templates` Python entry-point group — they appear in the registry after `pip install`. See the E1 spec and the `Template` protocol for authoring a template.

## Declarative grounding — `knowledge`

> Python only. Pins the agent to a specific OKF bundle directory instead of relying on the upward directory walk.

```toml
[tool.apx.agent]
name = "sales-coworker"
model = "databricks-claude-sonnet-4-6"
knowledge = "./.apx/okf"   # relative to project root
```

| Key | Type | Default | Description |
|---|---|---|---|
| `knowledge` | `str` | absent | Path to an OKF bundle directory, relative to the project root |

**Read-only grounding:** `knowledge` is a *read-only* runtime grounding source — the agent reads the OKF bundle at startup and never writes back to Unity Catalog. Writes to UC happen only through the governed `uc_comment_writer` tool, never as a side effect of grounding.

**Precedence:** When set, grounding loads directly from this bundle — it is the highest-priority baked grounding source, ahead of the automatic upward directory walk from the working directory. The path is resolved relative to the project root at startup.

**Graceful degradation:** If the path does not exist at startup (e.g. a freshly-cloned project before `apx-agent okf pull` has been run), grounding falls back silently to the cwd walk. The agent still starts.

**Project generation:** `apx-agent agents scaffold --no-yaml` can emit `knowledge = "./.apx/okf"` in the generated `pyproject.toml` **only when it also writes an `.apx/okf/` bundle** — i.e. when schema introspection succeeds and returns readable tables. If no tables are found (or auth fails), neither the knob nor the bundle is written, so the two are always coherent. The YAML-first path (`apx-agent agents scaffold` followed by `apx-agent agents deploy <spec>.yaml`) uses `generate_project` at deploy time and does not auto-emit this knob; set `knowledge:` explicitly in your YAML spec if you ship your own bundle.

## Declarative memory — `[tool.apx.agent.memory]`

> Python only. Declares a memory backend auto-attached on all runtimes (serve, log/deploy, `apx-agent agents describe`). Memory tools are additive over code-wired tools; code-wired tools win on name collision.

```toml
[tool.apx.agent]
name = "sales-coworker"
model = "databricks-claude-sonnet-4-6"

# in-memory (dev / tests)
[tool.apx.agent.memory]
type = "inmemory"

# or Lakebase (production):
# [tool.apx.agent.memory]
# type = "lakebase"
# host = "coworker-lakebase.db.databricks.com"
# database = "agentdb"
# embedding_model = "databricks-bge-large-en"
# embedding_dim = 1024
```

| Key | Type | Default | Description |
|---|---|---|---|
| `type` | `"inmemory" \| "delta" \| "lakebase"` | `"inmemory"` | Backend type |
| `embedding_model` | `str` | absent | Databricks serving-endpoint name for embeddings |
| `embedding_dim` | `int` | absent | Embedding dimensionality (required for lakebase) |
| `table_name` | `str` | absent | UC table (delta) or plain name (lakebase) |
| `database` | `str` | absent | Postgres database name |
| `host` | `str` | absent | Lakebase endpoint DNS; supports `$ENV_VAR` |
| `auto_create` | `bool` | `true` | Create table on first use |
| `ensure_extension` | `bool` | `true` | Run `CREATE EXTENSION IF NOT EXISTS vector` |
| `namespace_default` | `str` | `"default"` | Default namespace for memory tools |
| `tool_prefix` | `str` | `""` | Prefix for tool names (`"mem_"` → `"mem_recall"`) |
| `include` | `list[str]` | all | Subset: `["recall"]`, `["recall","remember"]`, … |
| `validate_at_boot` | `bool` | `true` | Reserved — boot-time connectivity validation is not yet implemented; field is accepted but has no effect |

**Principal isolation:** Memory is scoped per OBO user (`X-Forwarded-User`). User A's memories are invisible to User B. No-principal requests (local dev without headers) return `NO_PRINCIPAL` without writing.

**Deployment caveat — where `user_id` comes from:** Per-user memory only activates when the request carries a resolvable `user_id` (the principal). How that is supplied differs by serving surface:

- **Databricks Apps** (FastAPI `/invocations`): the `user_id` is bridged automatically from the `X-Forwarded-User` header injected by the Apps proxy. Memory activates with no caller action.
- **Pure Model Serving** (pyfunc served via `databricks.agents.deploy`, not routed through `/invocations`): the serving proxy does **not** inject `user_id`. The caller MUST pass it explicitly via `custom_inputs={"user_id": "<principal>", ...}` for per-user memory to activate. Without it, `recall`/`remember` return `NO_PRINCIPAL` and memory is inert (fail-closed — no cross-user leakage, but nothing is stored or recalled).

**Credential API:** Lakebase uses `ws.database.generate_database_credential` (the `DatabaseAPI`, not `PostgresAPI`).

An `[tool.apx.agent.example]` table (same fields, plus `agent_id`) declares a coworker-scoped few-shot example store — isolated by `agent_id` (defaults to the agent `name`), not per-user.

## Declarative session — `[tool.apx.agent.session]`

```toml
[tool.apx.agent.session]
type = "inmemory"
# or delta: type="delta", table_name="main.coworker.apx_sessions"
# or lakebase: type="lakebase", host="...", database="..."
```

**Precedence:** An explicit `create_app(conversation_store=X)` arg wins over config session (code is more specific intent). Config session is the fallback (e.g. template-only projects). `DeltaConversationStore` takes a three-part UC `table_prefix` (used as the base for the `_conversations` and `_items` tables); the wiring maps config `table_name` → `table_prefix` automatically.
