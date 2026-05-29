# Engine Scope: `[tool.apx.tools]` — Declarative Resource-Tool Loader

**Status:** Scope / design only (no implementation)
**Audience:** apx-agent engine maintainers; downstream consumer "Coworker" (agent-as-config)
**Author:** scoping pass, 2026-05-29

---

## 1. Goal & Motivation

Let an agent's *resource-reference-tier* tools be declared as **data** in
`pyproject.toml` instead of hand-written Python in `agent.py`. This is the core
of "skills as config" for **Coworker**, a config-driven consumer that builds
agent-as-config on top of apx-agent.

Today, to give an agent a Genie space tool, the AI engineer writes:

```python
# agent.py
from apx_agent import Agent, genie_tool, vector_search_tool

agent = Agent(tools=[
    genie_tool("01ef...", name="ask_sales", description="Ask the sales Genie space."),
    vector_search_tool("main.search.docs_index", columns=["title", "content"]),
])
```

Every one of these factories already takes **plain data args** (confirmed
below) and returns a stamped callable. There is no Python logic in that
`agent.py` worth writing by hand — it is pure configuration expressed as code.
This feature moves that configuration into TOML so a config generator (Coworker)
can emit it without generating or executing Python.

### Confirmed factory signatures (the "resource-reference tier")

All factories live in `python/src/apx_agent/` and end with a `build_tool(...)`
call (or set `__name__`/`__doc__` directly). Each takes its primary identifier
as the first positional arg and the rest as keyword-only data args:

| Factory | File:line | Signature (abbreviated) | Returns |
|---|---|---|---|
| `genie_tool` | `genie.py:82` | `(space_id, *, name="ask_genie", description=None)` | callable |
| `genie_query_tool` | `genie.py:128` | `(space_id, *, name="query_genie", description=None, max_rows=20)` | callable |
| `vector_search_tool` | `vector_search.py:15` | `(index_name, *, columns=None, num_results=5, filters_json=None, name="vector_search", description=None)` | callable |
| `uc_function_tool` | `catalog.py:192` | `(function_name, *, name=None, description=None, ws=None)` | callable |
| `uc_function_toolkit` | `catalog.py:317` | `(catalog_schema, *, ws=None, include=None, exclude=None)` | **list** |
| `catalog_tool` | `catalog.py:40` | `(catalog, schema, *, name="list_tables", description=None)` | callable |
| `schema_tool` | `catalog.py:144` | `(*, name="describe_table", description=None)` | callable |
| `lineage_tool` | `catalog.py:86` | `(*, name="get_table_lineage", description=None)` | callable |
| `sql_tool` | `sql_tools.py:23` | `(*, warehouse_id=None, name="run_sql", description=None, max_rows=1000)` | callable |
| `http_tool` | `http_tools.py:42` | `(connection, *, method="GET", path="", name="http_request", description=None, warehouse_id=None, max_chars=8000)` | callable |
| `openapi_tool` | `http_tools.py:199` | `(spec, connection, *, warehouse_id=None, include=None)` | **list** |
| `mcp_tool` | `mcp_consume.py:209` | `(server_url, tool_name, *, headers=None, transport="http", name=None, description=None)` | callable |
| `mcp_toolkit` | `mcp_consume.py:254` | `(server_url, *, headers=None, include=None, transport="http")` | **list** |
| `foundation_model_tool` | `foundation_model.py:22` | `(endpoint_name, *, system_prompt=None, temperature=None, max_tokens=None, name="ask_model", description=None)` | callable |
| `jobs_tools` | `jobs_tools.py:305` | `(*, warehouse_id=None)` | **list** (4 tools) |
| `jobs_for_table_tool` / `jobs_history_tool` / `jobs_logs_tool` / `jobs_source_paths_tool` | `jobs_tools.py` | `(*, name=..., description=..., [warehouse_id=...])` | callable |

**Key uniformity:** every factory accepts its first arg as positional-or-keyword
and everything else keyword-only. A flat `{type, <kwargs...>}` config maps onto
them with no special casing — the dispatcher passes the first positional arg by
its keyword name and splats the rest.

**Toolkit factories return lists** (`uc_function_toolkit`, `mcp_toolkit`,
`openapi_tool`, `jobs_tools`). The loader must flatten these into the tool list.

---

## 2. Precedent: how `sub_agents` flows from config to served tools

`sub_agents` is the existing "tools-as-config" mechanism and the template for
this feature — but it has a **governance gap at log time we must not copy**
(see §4).

Flow today:

1. **Config field** — `AgentConfig.sub_agents: list[str]` (`_models.py:68`),
   loaded from `[tool.apx.agent]` by `_load_agent_config` (`_inspection.py:179`,
   which filters to `AgentConfig.model_fields`).
2. **Constructor** — `LlmAgent.__init__(sub_agents=...)` stores
   `self._sub_agent_urls` (`_agents.py:110`).
3. **Merge at serve time** — `setup_agent` (`_wiring.py:85-110`) merges
   `config.sub_agents` into `agent._sub_agent_urls`, resolving `$ENV_VAR`
   refs (`_resolve_env_var`, `_wiring.py:47`), deduping, and **warning** if the
   root agent has no `_sub_agent_urls` attribute (composition roots can't hold
   them — `_wiring.py:92`).
4. **Served as tools** — `setup_agent` calls `agent.fetch_remote_tools()`
   (`_agents.py:206`), which builds an `AgentTool` per URL for the A2A card.
   At request time `RemoteDatabricksAgent` / the compile path turns each into a
   callable.
5. **Resources at log time** — `collect_resource_specs` (`_resources.py:275`)
   walks `_iter_sub_agents` (`_resources.py:236`) reading
   `agent._sub_agent_urls` directly. **This runs in the CLI deploy path
   (`cli.py:2485`) and `apx info` (`cli.py:3036`), NOT inside `setup_agent`.**

### The precedent's gap (verified)

`setup_agent` runs only inside the FastAPI lifespan (`_wiring.py:476`,
`create_app`). The CLI deploy/log path imports the agent via `_load_agent` and
walks `_tool_fns` / `_sub_agent_urls` **without ever running `setup_agent`**
(`cli.py:2485`, `cli.py:3036`). Therefore a sub_agent declared *only* in
`[tool.apx.agent].sub_agents` (not also in the constructor) is **absent from
the logged `resources=[...]`** — the deployed model gets a scoped token that
can't reach it. There is no test covering config-`sub_agents`-at-log-time
(grep of `python/tests/` confirms only constructor-path and env-resolution
tests). The gap is latent because for sub_agents the only missing resource is a
`serving_endpoint`, and many deployments declare the endpoint anyway.

**For this feature the gap is fatal.** Config-declared `genie_tool`,
`vector_search_tool`, `uc_function_tool`, `sql_tool` (pinned warehouse),
`http_tool`, `foundation_model_tool` each attach a *governed* `ResourceSpec`
(`genie_space`, `vector_search_index`, `uc_function`, `sql_warehouse`,
`uc_connection`, `serving_endpoint`) that the model **cannot access without
declaration**. If the merge runs only in `setup_agent`, every config-declared
governed tool is denied at runtime in production. The design therefore uses a
**single idempotent helper called from both the serve path and the
deploy/log/info path** (§3, §4).

---

## 3. Design

### 3.1 Config schema & placement decision

**Decision: read `[[tool.apx.tools]]` via its own section path; do NOT route it
through `AgentConfig`.**

Rationale: `_load_agent_config` filters to `AgentConfig.model_fields`
(`_inspection.py:179`), so an unknown key under `[tool.apx.agent]` is silently
dropped. `[[tool.apx.tools]]` is a *sibling* of `[tool.apx.agent]`, matching
the task's named schema and keeping the tool list out of the agent-identity
model. The loader reads it with section path `("tool", "apx", "tools")`,
mirroring the override hook already present in
`_load_agent_config(section_path=...)`.

> Alternative considered: `[[tool.apx.agent.tools]]` + a `tools: list[dict]`
> field on `AgentConfig`. Rejected — couples a *behavior* list to the
> *identity* model and forces every `AgentConfig` consumer (probe, watchdog,
> eval) to ignore a field they don't use. The standalone path is cleaner and
> matches the feature name.

The discriminator field is **`type`** (the factory's short name, e.g.
`"genie"`, `"vector_search"`). Each table is `{type, <factory kwargs>}`. The
first positional arg of each factory is given its real keyword name (e.g.
`space_id`, `index_name`, `connection`) so the mapping is 1:1 and self-documenting.

#### Concrete TOML examples

```toml
# pyproject.toml

[tool.apx.agent]
name = "sales-assistant"
model = "databricks-claude-sonnet-4-6"
instructions = "You help the sales team analyze pipeline data."

# --- Declarative resource tools (array of tables) ---

[[tool.apx.tools]]
type = "genie"
space_id = "01ef9c2a3b4c5d6e"
name = "ask_sales"
description = "Ask natural-language questions about the sales pipeline."

[[tool.apx.tools]]
type = "genie_query"          # genie_query_tool — structured rows
space_id = "01ef9c2a3b4c5d6e"
max_rows = 50

[[tool.apx.tools]]
type = "vector_search"
index_name = "main.search.docs_index"
columns = ["doc_id", "title", "content"]
num_results = 5

[[tool.apx.tools]]
type = "uc_function_toolkit"  # returns a LIST — flattened by the loader
catalog_schema = "main.agent_tools"
include = ["classify_intent", "lookup_account"]

[[tool.apx.tools]]
type = "sql"
warehouse_id = "abc123def456"
max_rows = 500

[[tool.apx.tools]]
type = "http"
connection = "salesforce_api"
method = "GET"
path = "/services/data/v59.0/query"
name = "salesforce_query"
description = "Run a SOQL query against Salesforce via the governed UC connection."

[[tool.apx.tools]]
type = "openapi"              # returns a LIST — flattened
spec = "https://petstore3.swagger.io/api/v3/openapi.json"
connection = "petstore_api"
include = ["pet"]

[[tool.apx.tools]]
type = "mcp_toolkit"         # returns a LIST — flattened; does LIVE discovery
server_url = "https://my-workspace.databricks.com/api/2.0/mcp/functions/main/tools"
include = ["forecast"]
transport = "http"

[[tool.apx.tools]]
type = "foundation_model"
endpoint_name = "databricks-claude-opus-4-7"
name = "ask_opus"
description = "Defer hard reasoning to the specialist model."
system_prompt = "You are a senior data engineer. Be terse."
temperature = 0.2

[[tool.apx.tools]]
type = "jobs"                # jobs_tools() — returns a LIST of 4
warehouse_id = "abc123def456"
```

`$ENV_VAR` / `${VAR}` substitution is supported on **string** values (reusing
`_resolve_env_var`, `_wiring.py:47`) so secrets-bearing fields like an MCP
`server_url` or `http` `connection` can be environment-driven for Coworker's
multi-tenant deploys. Substitution is applied recursively to string leaves
before dispatch.

### 3.2 Dispatcher design

**New module: `python/src/apx_agent/_tool_config.py`** (private, mlflow-free,
no heavy imports at module load — factories are imported lazily so the
dispatcher itself stays cheap, matching the lazy-import discipline already used
inside the factories).

**Registry: `type` string → factory callable + the keyword name of its first
positional arg.** A small declarative table, not per-type Pydantic models:

```python
# Illustrative — NOT to be committed verbatim; shows the shape.
_REGISTRY: dict[str, tuple[Callable, str | None]] = {
    "genie":              (genie_tool,            "space_id"),
    "genie_query":        (genie_query_tool,      "space_id"),
    "vector_search":      (vector_search_tool,    "index_name"),
    "uc_function":        (uc_function_tool,      "function_name"),
    "uc_function_toolkit":(uc_function_toolkit,   "catalog_schema"),
    "catalog":            (catalog_tool,          "catalog"),   # 2 positionals — special
    "schema":             (schema_tool,           None),
    "lineage":            (lineage_tool,          None),
    "sql":                (sql_tool,              None),
    "http":               (http_tool,             "connection"),
    "openapi":            (openapi_tool,          "spec"),       # 2 positionals — special
    "mcp_tool":           (mcp_tool,              "server_url"), # 2 positionals — special
    "mcp_toolkit":        (mcp_toolkit,           "server_url"),
    "foundation_model":   (foundation_model_tool, "endpoint_name"),
    "jobs":               (jobs_tools,            None),
    "jobs_for_table":     (jobs_for_table_tool,   None),
    "jobs_history":       (jobs_history_tool,     None),
    "jobs_logs":          (jobs_logs_tool,        None),
    "jobs_source_paths":  (jobs_source_paths_tool,None),
}
```

**Why a registry + generic dict, not per-type Pydantic models:**

- The factories are *already* the validation surface. They raise `ValueError`
  on bad input (`http_tool` validates `method` at `http_tools.py:91`;
  `uc_function_toolkit` validates the two-part identifier at `catalog.py:361`),
  fetch UC comments, and own their defaults. Re-declaring 16 Pydantic schemas
  duplicates those signatures and *will* drift from them.
- Python's own call machinery is the validator: splat the config dict as
  kwargs and let a `TypeError` (unexpected/missing kwarg) surface as a clear
  config error. The dispatcher wraps that in a `ToolConfigError` naming the
  offending table.
- Three factories take **two** positionals (`catalog_tool(catalog, schema)`,
  `openapi_tool(spec, connection)`, `mcp_tool(server_url, tool_name)`). These
  are handled by passing *all* config keys as keywords — every factory accepts
  its positionals as positional-*or-keyword*, so `genie_tool(space_id="...")`
  and `openapi_tool(spec="...", connection="...")` both work. This means the
  registry's "first-positional keyword name" entry is only used for friendlier
  error messages, not for the call itself; **the dispatcher passes everything
  by keyword.** That removes the multi-positional special case entirely.

**Dispatch algorithm** (`load_config_tools(raw_tables: list[dict]) -> list[Callable]`):

```
out = []
for i, table in enumerate(raw_tables):
    table = deep_resolve_env_vars(table)          # $VAR on string leaves
    type_ = table.pop("type", None)
    if type_ is None:  raise ToolConfigError(f"tool #{i}: missing 'type'")
    entry = _REGISTRY.get(type_)
    if entry is None:  raise ToolConfigError(f"tool #{i}: unknown type {type_!r}; known: {sorted(_REGISTRY)}")
    factory, _ = entry
    try:
        result = factory(**table)                 # everything by keyword
    except TypeError as e:                         # bad/missing kwarg
        raise ToolConfigError(f"tool #{i} (type={type_}): {e}") from e
    except <network/factory failures>:             # see §5 — policy per factory
        ...
    out.extend(result if isinstance(result, list) else [result])
return out
```

### 3.3 Integration point

**The crux (decided by §2's verified gap): a single idempotent merge helper
called from BOTH paths.**

New helper, e.g. in `_tool_config.py`:

```
def merge_config_tools(agent: BaseAgent, pyproject_path=None) -> None:
    """Load [[tool.apx.tools]] and append the resulting callables to the
    target LlmAgent's _tool_fns, deduping by __name__. Idempotent:
    safe to call twice (serve path may call after deploy path already did)."""
```

Behavior:

1. Read `[[tool.apx.tools]]` from pyproject (own section path; `tomllib`,
   reusing the discovery logic in `_load_agent_config`).
2. `load_config_tools(...)` → flat list of stamped callables.
3. Find the target `LlmAgent` (the leaf that owns `_tool_fns`). For a bare
   `LlmAgent`, that's the agent itself. For composition roots
   (`Sequential`/`Parallel`/`Router`/`Handoff`), config tools have no single
   home — **warn and skip**, exactly as `setup_agent` does for sub_agents on a
   non-`LlmAgent` root (`_wiring.py:92`). (Open question Q3: per-leaf targeting.)
4. **Dedup by `__name__`:** code-defined `tools=[...]` **win** on name
   collision; config is purely additive. Skip the config tool and `warning(...)`
   the conflict. This also makes the helper idempotent — a second call sees the
   config tools already present (by name) and skips them.
5. Append survivors to `agent._tool_fns`.
6. **Re-run analysis** (see §3.4) so `_analyzed` reflects the new tools.

**Call sites — anchor at chokepoints, do NOT enumerate CLI commands.**

Tracing every consumer of `_tool_fns` shows three *distinct runtimes* read it,
and only one of them runs `setup_agent`. Enumerating CLI call sites (the naive
"call it in the deploy command" approach) misses two of them:

| Runtime | What reads `_tool_fns` | Runs `setup_agent`? |
|---|---|---|
| **Serve (apps target)** | request-time `compile_to_langgraph` via `/invocations`; `collect_tools()` for A2A/MCP card | **Yes** — `mount_mcp_endpoints` → `setup_agent` (`_wiring.py:582`) |
| **Log / deploy** | `collect_resource_specs(agent)` → `mlflow_resources_for` → `mlflow.pyfunc.log_model(resources=...)` | **No** — `log_agent` (`_chat_agent.py:510`, `:527`) is a *public API* (`__init__.py:103`), called from the CLI (`cli.py:1789`) **and directly from notebooks/Coworker** |
| **Model-serving runtime** | predict-time `compile_to_langgraph(self._agent, ...)` (`_chat_agent.py:372`), where `self._agent` is captured at `log_agent` time (`_chat_agent.py:282`) | **No** — the logged MLflow model never runs the FastAPI lifespan |

The two non-serve runtimes are governance-critical:

- If the merge does **not** run before `collect_resource_specs`, governed
  config tools are **absent from the logged `resources=[...]`** → the model gets
  a scoped token that can't reach them → runtime denial. (This is §2's verified
  sub_agents gap.)
- If the merge does **not** run before `log_agent` captures `self._agent`, the
  config tool *callables* are **absent from `_tool_fns` at predict time** → the
  served model has the resources declared but the **LLM never sees the tools**.

Both failures are reached through `log_agent` / `mlflow_resources_for`, which
are public and not CLI-bound. **Therefore the merge must be anchored to the
agent-finalization chokepoint, not bolted onto specific commands.**

**Recommended anchor — a `finalize_agent(agent, pyproject_path=None)` helper**
(idempotent; runs `merge_config_tools`) called by:

1. **`log_agent` / `mlflow_resources_for`** *before* they walk the agent
   (`_chat_agent.py:527`, `:599`). This single site covers both the CLI deploy
   path *and* direct-from-notebook logging, and — because `log_agent` captures
   `self._agent` *after* finalization — guarantees config tool callables are
   present in the model-serving runtime too. This is the one site that, if
   wired correctly, fixes all three governance concerns at once.
2. **`setup_agent`** (`_wiring.py`), next to the existing `sub_agents` merge
   (`_wiring.py:85`), *before* `agent.collect_tools()` (`_wiring.py:112`) — so
   the apps-target A2A/MCP card and tool routes see config tools at serve time.
3. **`apx info`** (`cli.py:3013`), *before* `collect_resource_specs` — so the
   pre-deploy sanity check reports config tools.

Idempotency (§3, step 4) is what makes calling from three places safe: a second
finalize sees the config tools already present by `__name__` and skips them.

> **Caveat — `_resources.py` is documented mlflow-free / no-heavy-imports**
> (`_resources.py:21`). The merge pulls in the factories' Databricks SDK
> imports, which is acceptable at log/serve time but should **not** live inside
> `_resources.py`. Keep `finalize_agent` / `merge_config_tools` in
> `_tool_config.py` and call it from `_chat_agent.py` / `_wiring.py` / `cli.py`,
> leaving `collect_resource_specs` itself import-light.

Because everything downstream keys off `agent._tool_fns`
(`_compile.py:255` at run/predict time; `_iter_tool_fns`/`collect_resource_specs`
at log time; `_resources.py:202`), a single finalize before those reads makes
config tools flow into **compile, resource derivation, A2A card, MCP serving,
and per-tool routes for free** — provided `_analyzed` is also refreshed (§3.4).

> **No new `Agent` constructor param is required.** Merging post-construction
> via `_tool_fns` mutation is the lowest-risk integration and exactly mirrors
> how `setup_agent` already mutates `_sub_agent_urls`. The existing constructor
> path (`tools=[...]`) is untouched; config tools are strictly additive. A
> convenience constructor param (`load_config_tools=True`) is listed as an open
> question (Q2), not a requirement.

### 3.4 The `_analyzed` rebuild (load-bearing)

`LlmAgent.__init__` pre-analyzes tools into `self._analyzed` once at
construction (`_agents.py:124-129`). Two surfaces read `_analyzed`, **not**
`_tool_fns`:

- `collect_tools()` (`_agents.py:195`) → the A2A card + MCP tool list.
- `build_router()` (`_agents.py:177`) → per-tool FastAPI routes.

Appending to `_tool_fns` alone would make config tools **invisible** to the A2A
card, MCP surface, and tool routes. The merge helper must therefore, for each
appended tool, run `_inspect_tool_fn(fn)` + `_make_input_model(fn, plain_params)`
(`_agents.py:127-128`) and append the resulting tuple to `agent._analyzed`.

Recommended: add a small public-ish method on `LlmAgent`, e.g.
`_register_tool(fn)`, that appends to *both* `_tool_fns` and `_analyzed` in one
place, so the two lists cannot drift. The merge helper calls it per surviving
config tool. (Run-time compile reads `_tool_fns` directly, so that side is
already covered by the append.)

> Alternative considered: merge config tools *in the constructor* so `_analyzed`
> is built once over the full list. Rejected — config (pyproject path) is not
> available in `__init__` today, and threading it in would change the public
> constructor and break the "agent.py is pure Python, config is separate"
> separation Coworker depends on.

---

## 4. OBO / Governance

**OBO is preserved automatically.** Every factory injects the workspace client
via `UserClientDependency` (`_defaults.py`) into its inner async function
(`genie.py:110`, `vector_search.py:65`, `sql_tools.py:81`, `http_tools.py:113`,
`foundation_model.py:75`, `catalog.py:268`, etc.). That dependency is resolved
*per request* from the caller's `X-Forwarded-Access-Token` (bridged in
`setup_agent`'s `/invocations` route). Config-loaded tools are the **same
callables** produced by the same factories — there is no separate execution
path, so per-user OBO and per-user UC grants apply identically.

**Resource declarations AND tool callables are preserved — IF the merge runs at
the `log_agent` chokepoint (§3.3).** Factories attach `ResourceSpec` via
`build_tool(..., resources=[...])` (`_tool_factory.py:69` → `attach_resources`),
stored as `_apx_resources` on the callable. `collect_resource_specs` reads that
attribute while walking `_tool_fns` (`_resources.py:231`, `_resources.py:310`),
and the model-serving runtime compiles the same `_tool_fns` at predict time
(`_chat_agent.py:372`). **This is exactly why §3.3 anchors the merge at
`log_agent` / `mlflow_resources_for`** — finalizing before the agent is captured
makes both the logged `resources=[...]` *and* the served tool surface complete.
Without it, governed config tools attach their `ResourceSpec` to a callable the
resource walker never sees (→ runtime denial) and the predict-time compile never
sees the tool (→ LLM can't call it). Tools
covered: `genie`(genie_space), `vector_search`(vector_search_index),
`uc_function`/`uc_function_toolkit`(uc_function), `sql`/`jobs`(sql_warehouse,
when pinned), `http`/`openapi`(uc_connection + optional sql_warehouse),
`foundation_model`(serving_endpoint).

**MCP tools declare NO resources** (`_make_mcp_tool` calls `build_tool` with no
`resources=`, `mcp_consume.py:206`). They reach a remote server over HTTP, not a
governed Databricks resource, so their absence from the logged resource list is
*correct* — which conveniently means the factories most likely to fail at boot
(§5) don't need to be present at log time anyway.

### Security concerns with config-declared endpoints

The decisive question: **is `[[tool.apx.tools]]` developer-authored (trusted)
or generated by Coworker from downstream/user input (untrusted)?**

- **`openapi`** fetches an arbitrary `spec` URL at factory time
  (`_load_openapi_spec` → `httpx.get`, `http_tools.py:177`) from the app's
  network position. If `spec` can be influenced by untrusted input, this is a
  classic **SSRF** vector (cloud metadata endpoints, internal services).
- **`mcp_toolkit` / `mcp_tool`** point the agent at an arbitrary `server_url`
  and connect at factory time (`_run_sync(_list_remote_tools)`,
  `mcp_consume.py:278`). The existing `_same_origin` guard
  (`mcp_consume.py:85`) only prevents **OBO-token leakage** to a foreign
  origin; it does **not** restrict the *connection target* — an arbitrary
  `server_url` is still contacted (without the token).
- **`http` / `openapi`** route through a governed UC `connection`, so the
  egress *credential* and host are UC-governed; the residual risk is the
  factory-time spec fetch above, not the request path.

**Recommendation (state the trust model explicitly in the loader docs):**

1. **Default assumption: `pyproject.toml` is developer-authored and trusted**,
   same trust level as `agent.py`. Under this assumption no allow-listing is
   required (and none exists for the equivalent hand-written factories today).
2. **For Coworker's untrusted/generated config**, gate the network-touching
   types (`openapi`, `mcp_tool`, `mcp_toolkit`) behind an **opt-in allow-list**:
   an env var (e.g. `APX_TOOLS_ALLOWED_HOSTS`) or a `[tool.apx.tools]`-level
   policy block that the loader enforces on `spec`/`server_url` hosts before the
   factory-time fetch. When set, reject out-of-list hosts with `ToolConfigError`.
   When unset, preserve today's trusted behavior (no regression).
3. Independent of trust: keep the existing `_UNTRUSTED_NOTE` labeling on remote
   MCP tool descriptions/results (`mcp_consume.py:111`) — already in place.

---

## 5. Error handling

Three failure classes, three policies:

1. **Bad `type` (unknown discriminator).** Fail loudly: raise `ToolConfigError`
   listing the offending table index and the set of known types. This is a
   config typo, not a transient condition — startup/deploy should not proceed
   silently.

2. **Missing/extra required arg.** `factory(**table)` raises `TypeError`. Wrap
   in `ToolConfigError` naming the table index, `type`, and the original
   message (e.g. *"tool #3 (type=genie): missing required argument: 'space_id'"*).
   Loud failure — a config-time mistake.

   **Config-vs-config name collision.** Two `[[tool.apx.tools]]` of the same
   type without an explicit `name` produce the *same* `__name__` (two
   `vector_search` tables → both `"vector_search"`). Unlike a config-vs-*code*
   collision (§3.3, where code wins silently-with-warning), two config tools
   with the same name is an authoring bug that would either drop the second or
   register a duplicate LLM tool name (breaking the tool schema). Detect this
   in the loader and raise `ToolConfigError` requiring an explicit `name=` on
   one of them.

3. **Factory-time runtime failures (network / live discovery).** Only **three**
   factories do I/O at factory time:
   - `mcp_toolkit` / `mcp_tool` — live `list_tools` over the network
     (`mcp_consume.py:278`, `:239`). Server down at boot → exception.
   - `openapi_tool` — `httpx.get(spec)` (`http_tools.py:177`). Unreachable
     spec URL → exception.
   - `uc_function_toolkit` — `ws.functions.list(...)` (`catalog.py:373`). Note:
     it **already** catches and returns `[]` on failure (`catalog.py:374`), so
     a down metastore degrades to an empty toolkit, not a crash.

   All other types (`genie`, `vector_search`, `sql`, `http`,
   `foundation_model`, single `uc_function_tool` without `ws`, `jobs`,
   `catalog`/`schema`/`lineage`) are **pure data construction** — they cannot
   fail at boot for connectivity reasons.

   **Policy for the I/O factories: per-table `try/except`, skip-with-warning.**
   A transiently-down MCP server or unreachable OpenAPI spec must not take the
   whole agent down at boot — degrade to "that toolkit is unavailable this
   boot" and log a `warning`, matching `setup_agent`'s
   `resolved-empty → skip` handling for sub_agents (`_wiring.py:104`) and
   `uc_function_toolkit`'s own list-fails-to-`[]` behavior. A
   **strict mode** flag (env var, e.g. `APX_TOOLS_STRICT=1`) can promote these
   to hard failures for CI/deploy validation where a missing tool *should*
   block the release.

   **Log-path consideration:** because MCP tools declare no resources (§4) and
   `uc_function_toolkit` already self-heals to `[]`, skip-with-warning at deploy
   time does not silently drop a governed resource for the failing factories —
   the only governed-resource toolkit (`uc_function_toolkit`) fails closed to
   empty by its own design, which `strict mode` should flag.

---

## 6. Testing plan

**Unit (`python/tests/test_tool_config.py` — new):**

- **Dispatch happy path, one per type:** a parametrized test asserting each
  registry `type` builds the right callable. Monkeypatch the three I/O factories
  (`mcp_toolkit`, `openapi_tool`, `uc_function_toolkit`) so the unit tests stay
  offline; assert the dispatcher calls them with the splatted kwargs.
- **Toolkit flattening:** `uc_function_toolkit` / `mcp_toolkit` / `openapi` /
  `jobs` return lists → assert the loader flattens into `_tool_fns`.
- **First-positional-as-keyword:** assert `{type="genie", space_id="x"}`
  reaches `genie_tool(space_id="x")` and the two-positional cases
  (`catalog`, `openapi`, `mcp_tool`) build correctly via all-keyword calls.
- **Env var substitution:** `server_url = "$MCP_URL"` resolves from the
  environment before dispatch; unset → `ToolConfigError` or skip per policy.
- **Errors:** unknown `type` → `ToolConfigError`; missing required arg →
  `ToolConfigError` (wrapping `TypeError`); factory-time exception under default
  mode → skip + warning; under strict mode → raise.
- **Dedup/precedence:** config tool with the same `__name__` as a code-defined
  tool is dropped with a warning; code tool wins. Two config tools with the same
  resolved `__name__` → `ToolConfigError` (§5).
- **Idempotency:** calling `finalize_agent(agent)` twice yields the same
  `_tool_fns` / `_analyzed` (no duplicates) — guards the three-call-site safety.
- **`_analyzed` refresh:** after merge, `agent.collect_tools()` includes the
  config tools (proves the A2A/MCP surface sees them, not just compile).
- **Resource preservation:** after merge, `collect_resource_specs(agent)`
  includes the `ResourceSpec`s of governed config tools (genie_space,
  vector_search_index, etc.). **This is the regression guard for §2's gap.**
- **Composition-root skip:** config tools on a `SequentialAgent` root →
  warning + no crash (mirror `_wiring.py:92`).

**Integration (`python/tests/test_wiring.py` + `test_chat_agent.py` extensions):**

- **Serve path (apps target):** build a real `LlmAgent`, write a temp
  `pyproject.toml` with `[[tool.apx.tools]]`, run `setup_agent`, assert the
  resulting `AgentContext` card lists the config tools and `/api/tools/<name>`
  routes exist.
- **Log path (governance regression — §2's gap):** finalize an agent + config
  via `mlflow_resources_for` / `log_agent` (the public, non-CLI entry point),
  then assert the derived resources include the governed config tools'
  `ResourceSpec`s (genie_space, vector_search_index, uc_function, ...). Would
  fail under a `setup_agent`-only merge. This is the test the sub_agents
  precedent lacks; it locks the fix in.
- **Model-serving predict path:** after `log_agent` finalization captures
  `self._agent`, assert `compile_to_langgraph(self._agent, ...)` /
  `agent._tool_fns` includes the config tool callables — i.e. the served model's
  LLM can actually *see* the config tools, not just declare their resources.
  (Mock the LLM; assert the tool is bound to the compiled graph.)
- **`apx info`:** with config tools present, `apx info` lists them and their
  resources.

---

## 7. Effort estimate & open questions

### Effort: **M** (≈ 350–550 LOC incl. tests)

| Component | ~LOC |
|---|---|
| `_tool_config.py` (registry, `load_config_tools`, `merge_config_tools`, env-resolve, errors) | 120–160 |
| `LlmAgent._register_tool` (append to `_tool_fns` + `_analyzed`) | 15–25 |
| `finalize_agent` chokepoint wiring (`log_agent`/`mlflow_resources_for` + `setup_agent` + `apx info`) | 20–35 |
| `LlmAgent._register_tool` (append to `_tool_fns` + `_analyzed`) | 15–25 |
| Optional allow-list / strict-mode plumbing | 20–40 |
| Unit + integration tests | 150–250 |

Not L because no new runtime/execution path is introduced — config tools reuse
the existing factories and the existing `_tool_fns` plumbing end to end. The
genuine work is the **chokepoint-anchored idempotent merge (`log_agent` +
`setup_agent` + `info`) + `_analyzed` refresh + the log-path/predict-path
regression tests**, not new tool machinery.

### Open questions

- **Q1 — Section name:** confirm `[[tool.apx.tools]]` (sibling) vs
  `[[tool.apx.agent.tools]]` (nested under agent). This doc picks the sibling
  to match the feature name and avoid `AgentConfig` model coupling. Coworker's
  config emitter should confirm which it prefers to generate.
- **Q2 — Constructor convenience:** offer `LlmAgent(..., load_config_tools=True)`
  as sugar, or keep merging purely external (CLI/`setup_agent`)? External-only
  is lower-surface; the param is nice for notebook users.
- **Q3 — Composition-root targeting:** for `Sequential`/`Parallel`/`Router`,
  config tools currently have no home (warn + skip). Do we need per-leaf
  targeting (e.g. `[[tool.apx.tools]]` with an `agent = "<leaf-name>"` key)?
  Likely a v2 concern; Coworker's first agents are flat `LlmAgent`s.
- **Q4 — Trust model default:** confirm whether Coworker-generated
  `pyproject.toml` should be treated as trusted (no allow-list) or untrusted
  (allow-list on by default for `openapi`/`mcp_*`). Decides whether §4's
  allow-list is opt-in or opt-out.
- **Q5 — Should `mcp_*`/`openapi` be excluded from the `log_agent`-time finalize
  entirely** (they declare no resources and do network I/O), running only at
  serve time? This would make logging fully offline for those types. Trade-off:
  the served model-serving runtime would then *also* lack those tool callables
  (predict reads the captured `_tool_fns`), so the LLM couldn't call them on the
  model-serving target — they'd only work on the apps target. Net: the I/O types
  are genuinely a model-serving-vs-apps capability question, not just a
  cosmetic `apx info` one. Recommend including them in finalize with
  skip-with-warning (§5), and using strict mode to gate deploys where their
  availability is required.
- **Q6 — Fix the sub_agents precedent gap?** This design adds the missing
  log-path merge for *tools*; the equivalent gap for *config sub_agents
  resources* remains. Worth a follow-up to route both through the shared helper.
