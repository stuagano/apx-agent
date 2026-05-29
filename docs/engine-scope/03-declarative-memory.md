# Engine Scope 03 — Declarative Memory + Session Backend Config

Status: **Scope / design only** (no implementation in this doc).
Audience: apx-agent engine maintainers + the downstream "Coworker" config-driven consumer.

## 1. Problem & goal

Today an agent's memory, example, and session backends are **code**, hand-wired
by the user at construction time:

- **Memory / examples** reach the agent ONLY through
  `make_memory_tools(store=...)` / `make_example_tools(store=...)`
  (`python/src/apx_agent/_memory_tools.py:68`, `_example_tools.py:67`). The user
  calls these in `app.py` and threads the result into `Agent(tools=[...])`. The
  store object and its `principal_id_resolver` closure are captured at
  construction.
- **Sessions** reach the runtime through a Python argument:
  `create_app(agent, session_store=...)`
  (`_wiring.py:434`) → `mount_invocations_route(..., session_store=...)`
  (`_wiring.py:486` → `_invocations.py:71`).

The config keys `[tool.apx.agent].memory_store` / `example_store` already exist
but are read **only by the CLI** (`_load_store`, `cli.py:4463-4503`) for the
offline `apx memory` / `apx example` commands. They point at a Python object
(`MODULE:VAR`) — i.e. still code, just late-bound. **The served runtime never
reads them.** (Confirmed: grep shows no reference to `memory_store` /
`example_store` outside `cli.py`; `configuration.md` does not document them.)

**Goal:** let memory / example / session backends be declared as **data** in
`pyproject.toml` (e.g. `[tool.apx.agent.memory] type = "lakebase"`) and be
**auto-attached** to the served agent — so a downstream consumer can stamp out
many "coworkers" from a spec, each with its own declared memory, without writing
`make_memory_tools` glue per coworker.

### Why this is non-trivial (two load-bearing facts, verified in code)

1. **Tools must be merged into the executed graph, not just the discovery card.**
   `setup_agent` builds `tools = agent.collect_tools()` (`_wiring.py:112`) purely
   for the A2A card + per-tool routes. The agent the runtime *actually executes*
   is compiled per-request from `agent._tool_fns` (`_compile.py:255`,
   `:461`, `:624`). **Appending to the card list alone would advertise the
   memory tools but the LLM could never call them.** The attachment point must
   mutate `agent._tool_fns` (a plain mutable list, `_agents.py:109`) *before*
   the first request compiles the graph.

2. **Per-request principal is not reachable from a config-built memory tool.**
   `make_memory_tools` resolves the principal per call via a zero-arg
   `principal_id_resolver: Callable[[], str | None]` (`_memory_tools.py:71`,
   `:116-122`). In code-wired usage the *user* supplies that resolver. In the
   config path the framework must supply one — and it must read the current
   request's OBO identity. But inside a compiled LangGraph there is **no FastAPI
   `Request`** (`_compile.py:104-106`: `_get_request` is intentionally
   unresolvable). The per-request identity lives on the `CompileContext`
   (`_compile.py:77-87`: `ctx.ws`, `ctx.headers`) which is built fresh per
   request, but tool closures are plain functions that don't receive `ctx`.
   **There is no existing mechanism for a zero-arg resolver to read the current
   principal.** Closing this gap (a request-scoped `ContextVar`, or a new
   injected dependency) is the central new primitive this feature requires —
   see §4.

## 2. Config schema

### 2.1 Placement decision: sub-tables under `[tool.apx.agent]`

Use **sub-tables of the agent section**, not a top-level `[tool.apx.memory]`:

```toml
[tool.apx.agent.memory]   # not [tool.apx.memory]
[tool.apx.agent.example]
[tool.apx.agent.session]
```

Rationale:
- The existing `memory_store` / `example_store` keys already live under
  `[tool.apx.agent]` — keep the family together.
- The engine model is **one agent per pyproject section**
  (`_load_agent_config`, `_inspection.py:127-179`). Memory is not shared across
  multiple agents in one project, so a top-level table earns nothing.
- A top-level table would only make sense if several agents in one project
  shared a store — not this model.

### 2.2 `AgentConfig` must gain typed fields

`_load_agent_config` drops any key not in `AgentConfig.model_fields`
(`_inspection.py:179`:
`{k: v for k, v in section.items() if k in AgentConfig.model_fields}`). So the
new sub-tables are invisible unless `AgentConfig` (`_models.py:57`) gains typed
fields:

```python
class AgentConfig(BaseModel):
    ...
    memory: MemoryBackendConfig | None = None
    example: ExampleBackendConfig | None = None
    session: SessionBackendConfig | None = None
```

(Nested pydantic models parse the TOML sub-tables automatically.)

### 2.3 Discriminated backend models

```python
StoreType = Literal["inmemory", "delta", "lakebase"]

class MemoryBackendConfig(BaseModel):
    type: StoreType = "inmemory"

    # --- shared / embedding ---
    # Endpoint NAME of a Databricks embeddings model. Required for lakebase
    # (pgvector needs vectors) and for delta when semantic recall is wanted.
    # See §2.4 — config cannot carry a Python callable, so the engine builds
    # the batched embedder from this endpoint name.
    embedding_model: str | None = None        # e.g. "databricks-bge-large-en"
    embedding_dim: int | None = None          # required by lakebase; informational for delta

    # --- delta ---
    table_name: str | None = None             # FQN "catalog.schema.apx_memories"
    index_name: str | None = None             # optional Vector Search index
    auto_create: bool = True

    # --- lakebase ---
    instance_name: str | None = None          # Lakebase/Database instance to mint creds for
    database: str | None = None               # postgres db name
    host: str | None = None                   # optional explicit host ($ENV_VAR supported)
    # table_name reused (defaults "apx_memories"); auto_create / ensure_extension
    ensure_extension: bool = True

    # --- behavioral ---
    namespace_default: str = "default"
    tool_prefix: str = ""                     # e.g. "memory_" to avoid name clashes
    include: list[str] | None = None          # subset: ["recall","remember","forget"]
```

`ExampleBackendConfig` mirrors this but isolates by **`agent_id`** (not
principal) and tools are `find_examples` / `save_example` / `remove_example`
(`_example_tools.py`). It needs an `agent_id` (defaults to `config.name`) and
the same backend connection fields.

`SessionBackendConfig` only needs backend + connection (no tools, no
embeddings):

```python
class SessionBackendConfig(BaseModel):
    type: StoreType = "inmemory"
    table_name: str | None = None      # delta: FQN; lakebase: "apx_sessions"
    instance_name: str | None = None   # lakebase
    database: str | None = None
    host: str | None = None
    auto_create: bool = True
```

Note the constructor-arg name mismatch the wiring must bridge: `DeltaSessionStore`
takes **`table_path`** (`_session_delta.py:83`), not `table_name`. Keep
`table_name` as the uniform config key and map it to `table_path` when building
the Delta session store. (`LakebaseSessionStore` uses `table_name`,
`_session_lakebase.py:107`.)

### 2.4 Concrete TOML examples

In-memory (dev / tests — zero external deps):

```toml
[tool.apx.agent.memory]
type = "inmemory"
```

Delta (Unity Catalog table; runs as the request's OBO principal via SQL):

```toml
[tool.apx.agent.memory]
type = "delta"
table_name = "main.coworker.apx_memories"
embedding_model = "databricks-bge-large-en"
embedding_dim = 1024
auto_create = true

[tool.apx.agent.session]
type = "delta"
table_name = "main.coworker.apx_sessions"
```

Lakebase (pgvector):

```toml
[tool.apx.agent.memory]
type = "lakebase"
instance_name = "coworker-lakebase"
database = "agentdb"
table_name = "apx_memories"
embedding_model = "databricks-bge-large-en"
embedding_dim = 1024

[tool.apx.agent.example]
type = "lakebase"
instance_name = "coworker-lakebase"
database = "agentdb"
table_name = "apx_examples"
embedding_model = "databricks-bge-large-en"
embedding_dim = 1024
```

`$ENV_VAR` / `${VAR}` references should be resolved with the existing
`_resolve_env_var` helper (`_wiring.py:47`) for `host` / `instance_name` /
`database` (deploy-environment portability).

### 2.5 What config CANNOT express — embedding & engine provenance

- **`embedding_fn` is a Python callable** and `_memory.py:21` notes there is
  **no built-in embedder** in the package — the caller wires it. Config carries
  only `embedding_model` (an endpoint name); the engine must **build** the
  batched `embedding_fn` from it (call the Databricks embeddings endpoint via
  the workspace client). This is **net-new code** (an `_embeddings.py` helper:
  `make_embedding_fn(ws, endpoint_name) -> EmbeddingFn`). Counted in §6.
- **Lakebase needs a SQLAlchemy `Engine` with a `do_connect` OAuth listener**
  that mints fresh creds via `generate_database_credential(instance_names=[...])`.
  Config cannot carry an `Engine`; the engine must **build** it from
  `instance_name` / `database` / `host` plus the app's workspace client. Also
  net-new (an `_lakebase_engine.py` helper). Counted in §6.
  - **Reconcile the credential API first:** the codebase is inconsistent — memory
    docs use `ws.postgres.generate_database_credential(..., request_id=...)`
    (`_memory_lakebase.py:236`) while session docs use
    `ws.database.generate_database_credential(...)` (`_session_lakebase.py:94`).
    The shared `_lakebase_engine.py` builder must pick ONE (confirm against the
    installed `databricks-sdk` version) rather than copying both.
- **Delta needs a `run_sql` callable** (`_memory_delta.py:244`). The engine
  already has `run_sql(ws, q)` (`_compile.py:103`, `_sql.run_sql`) — reuse it,
  bound to the **service-principal** app client for write-time DDL/MERGE, OR to
  the per-request OBO client (see §4 for the isolation trade-off).

## 3. Instantiation + attachment

### 3.1 Where the engine reads config & builds stores

The work splits into **two distinct responsibilities with different timing**,
because of how the discovery card is built:

- **Tool-merge (memory + example)** must run **inside `setup_agent`, before
  `_wiring.py:112`** — `setup_agent` snapshots `agent.collect_tools()` into
  `card` / `ctx.card` at `_wiring.py:112-128`, and `/.well-known/agent.json`
  serves that *frozen* card object (`_wiring.py:308`). If memory tools are
  merged into `agent._tool_fns` only *after* `setup_agent` returns, they become
  **callable** (the graph reads live `agent._tool_fns`, `_compile.py:255`) but
  are **absent from the served card**. So the merge must precede the snapshot.
  - Concretely: add `attach_declared_tools(agent, config)` near the top of
    `setup_agent`, just before `tools = agent.collect_tools()` (`_wiring.py:112`).
    It builds the memory/example tools and appends them via a new
    `LlmAgent.add_tools(fns)` (§3.1.1). The dedup-by-name check (§3.4) still
    works here: code-wired tools are already in `agent._tool_fns` from
    `__init__` (`_agents.py:109`), so the collision is visible.
- **Session-store resolution** has no card interaction and stays in the
  **lifespan after `setup_agent`**, feeding `mount_invocations_route`
  (which runs later — `_wiring.py:486`). A separate
  `resolve_session_store(config, override) -> SessionStore | None`.

Proposed module `_memory_wiring.py` with two entry points:

```python
def attach_declared_tools(agent: BaseAgent, config: AgentConfig, ws) -> None:
    """Build config-declared memory/example tools and merge them into the agent
    via agent.add_tools(...). Called INSIDE setup_agent before the card snapshot.
    Idempotent; no-op when nothing declared. Skips name collisions (§3.4)."""

def resolve_session_store(config: AgentConfig, override, ws):
    """Return override if not None, else build the config-declared session store
    (or None). Called in the lifespan, fed to mount_invocations_route."""
```

Integration points:

1. **Inside `setup_agent`** (`_wiring.py:59-159`): call
   `attach_declared_tools(agent, config, ws)` **before** `_wiring.py:112`. This
   is the single place that guarantees both *callable* and *carded*. It covers
   BOTH the `create_app` path and the `mount_mcp_endpoints` apps-target path,
   since both call `setup_agent` (`_wiring.py:476`, `:582`) — no per-call-site
   duplication needed for tools.
2. **`create_app` lifespan** (`_wiring.py:459-488`): replace the hardcoded
   `session_store=session_store` (`_wiring.py:486`) with
   `resolve_session_store(ctx.config, override=session_store, ws=...)` and pass
   the result into `mount_invocations_route(..., session_store=...)`.
   - **Precedence:** an explicit `create_app(session_store=...)` arg **wins**
     over config (override beats data — code is more specific intent).
3. **`mount_mcp_endpoints` apps-target path** (`_wiring.py:567-602`): tools are
   already handled via `setup_agent` (point 1). This path does NOT mount
   `/invocations` itself (the `AgentServer` does), so config sessions here only
   matter if/when that path consumes the resolved store — see Q4. Document that
   for now.

#### 3.1.1 `LlmAgent.add_tools(fns)` — keep `_analyzed` and the card in sync

Prefer a real method over poking `_tool_fns` directly. It appends to
`agent._tool_fns` AND re-runs the per-fn analysis the constructor does
(`_agents.py:124-129`) so `_analyzed` / `build_router` / `collect_tools` stay
consistent. Because it runs before the card snapshot (point 1), the merged tools
flow into `ctx.card` automatically. The `ws` is needed at this point to build
the embedding fn / Delta `run_sql` / Lakebase engine (§2.5) — at startup the SP
client (`app.state.workspace_client`, `_wiring.py:473`) is available; per-request
OBO is irrelevant to tool *construction* (the principal is resolved per call,
§4).

**Ordering recap (the binding constraint):** the merge must happen *before the
card snapshot* at `_wiring.py:112` (for card visibility) which is itself well
before the first `/invocations` request compiles the graph (for callability).
Placing `attach_declared_tools` near the top of `setup_agent` satisfies both at
once. Always go through `LlmAgent.add_tools(fns)` (§3.1.1) rather than poking
`_tool_fns`, so `_analyzed` (`_agents.py:124-129`), `build_router`, and
`collect_tools` all stay consistent.

### 3.2 Tool construction in the config path

For memory: build the store (§3.3), then
`make_memory_tools(store=store, principal_id_resolver=<framework resolver,
§4>, namespace_default=cfg.namespace_default, tool_prefix=cfg.tool_prefix,
include=cfg.include)` and append the returned fns. Same pattern for examples
with `make_example_tools` + an `agent_id_resolver` (defaulting to a constant
`config.name` — examples are agent-scoped, not user-scoped).

### 3.3 Store factory

A `_build_store(kind, cfg, ws)` dispatch on `cfg.type`:
- `inmemory` → `InMemoryMemoryStore(embedding_fn=...)` (`_memory.py:329`)
- `delta` → `DeltaMemoryStore(run_sql=..., embedding_fn=..., embedding_dim=...,
  table_name=cfg.table_name, auto_create=cfg.auto_create, index_name=cfg.index_name)`
  (`_memory_delta.py:241`)
- `lakebase` → build engine (§2.5) → `LakebaseMemoryStore(engine=..., embedding_fn=...,
  embedding_dim=..., table_name=cfg.table_name, auto_create=cfg.auto_create,
  ensure_extension=cfg.ensure_extension)` (`_memory_lakebase.py:246`)

Validation: each `type` requires a specific field set (see §5). Fail fast with a
clear message naming the missing key.

### 3.4 Coexistence with code-wired memory — three sources, dedup by tool name

There are now **three** ways memory tools can exist:
1. **Code-wired** — user called `make_memory_tools` in `app.py` and passed the
   result into `Agent(tools=[...])`. Already in `agent._tool_fns`.
2. **CLI `memory_store` MODULE:VAR** — code ref, offline CLI only, unaffected by
   this feature (different surface).
3. **Declarative `[tool.apx.agent.memory]`** — new, built by the engine.

The minted names collide: `recall` / `remember` / `forget` (and example
equivalents). **Precedence rule: code-wired wins.** When
`attach_declared_tools` is about to add a tool whose name already exists in
`agent._tool_fns`, **skip it and log a warning**:

```
[tool.apx.agent.memory] declares 'recall' but the agent already wires a tool
named 'recall' — keeping the code-wired tool, ignoring the declared one.
Use tool_prefix to mount both.
```

Rationale: code is the more specific, intentional act; silently overriding it
would surprise users mid-migration. `tool_prefix` is the escape hatch to mount
both. (Dedup is purely by tool `__name__`; no attempt to detect "same store".)

## 4. OBO / multi-tenancy — the principal isolation gap

### 4.1 Isolation is row-level, by key — NOT table-level

- **Memory** partitions by the `principal_id` **column** (`recall`/`add`
  filter on it — `_memory_tools.py:145-153`, store schemas have
  `principal_id` + an index, `_memory_lakebase.py:290`,`:301`). One shared table
  serves all users; isolation is `WHERE principal_id = :caller`.
- **Examples** partition by `agent_id` — i.e. by *coworker*, not by end user.
- **"Per-coworker memory"** therefore means **one table partitioned by key**,
  NOT a table per coworker. Teardown = `DELETE WHERE <key>`, not `DROP TABLE`.
- **Do not conflate the two keys.** For a multi-tenant runtime serving many
  coworkers: the **coworker** is the `agent_id` (examples) / the served agent
  identity; the **end user** is the `principal_id` (memory). A single coworker's
  memory is still sub-partitioned per end user.

### 4.2 The gap: a config-built tool cannot see the per-request principal

As established in §1(2): `make_memory_tools` needs
`principal_id_resolver() -> str | None`, but inside the compiled graph there is
no `Request` (`_compile.py:104-106`), and tool closures don't receive the
`CompileContext` that *does* carry per-request identity
(`ctx.headers` → `DatabricksAppsHeaders.user_id` from `X-Forwarded-User`,
`_defaults.py:67`; `ctx.ws` is the OBO client). A naive
`principal_id_resolver=lambda: None` collapses every user into the
`NO_PRINCIPAL` path (`_memory_tools.py:36`) — **no memory at all**, and
critically **no cross-user leak** (fails safe, but useless).

### 4.3 Recommended fix: a request-scoped principal `ContextVar`

Add a `ContextVar[str | None]` (e.g. `apx_agent._principal.current_principal`):

- **Set it per request** in the compile/run path where the `CompileContext` is
  built with OBO identity. `_compile_run.py` builds `ws`/headers per request
  (`_resolve_request_ws`, `:56`, used at `:181`,`:212`); set the contextvar from
  `ctx.headers.user_id` (or derive from the OBO token) at the same point, in a
  `try/finally` (or `contextvars.copy_context`) so it's reset after the turn.
  Because the sync tool bridge may hop threads (`_compile.py:182-186`), prefer
  binding the value into the resolved-deps closure rather than relying on a
  thread-local — see alternative below.
- **Framework resolver reads it:**
  `principal_id_resolver = lambda: current_principal.get()`.

This keeps `make_memory_tools` unchanged and preserves per-user isolation: each
request runs in its own context, the resolver returns that request's principal,
the store filters by it.

**Thread-hop caveat:** the async→sync bridge in `_make_langchain_tool`
(`_compile.py:182-186`) runs the coroutine in a `ThreadPoolExecutor`, where a
plain `ContextVar` does NOT propagate. Mitigation options (pick one in design
review):
  (a) capture `contextvars.copy_context()` and run the worker via
      `ctx.run(...)`; or
  (b) **preferred — no contextvar at all:** treat the principal like the other
      injected deps. Register a resolver in `_make_dep_resolvers`
      (`_compile.py:95-107`) that yields the per-request principal from
      `ctx.headers`, and have the config path build memory tools whose principal
      arrives as a resolved dependency (a new `Dependencies.Principal`), so it's
      captured in the same closure as `ctx.ws` and survives the thread hop
      automatically. This reuses the proven OBO-closure pattern
      (`_compile.py:150-160`) and avoids contextvar/thread pitfalls entirely.

Option (b) is the cleaner long-term design but requires the config-path memory
tools to use a dependency-injected principal rather than the existing zero-arg
resolver — a small variant factory or a `Dependencies.Principal`-aware wrapper.
Option (a) is the smaller diff. **Recommendation: (b).**

### 4.4 Isolation must be verified, not assumed

Whichever option ships, the design's isolation claim is only *true* once the
principal actually threads through. Until then, §4.2's fail-safe (no principal →
no memory) is the guarantee. Section 6 lists the isolation test as mandatory.

## 5. Error handling

- **Missing connection config:** validate per-`type` at build time
  (startup), fail with a precise message:
  `[tool.apx.agent.memory] type="lakebase" requires instance_name and database`.
  Lakebase also requires `embedding_model` + `embedding_dim`
  (`LakebaseMemoryStore` raises `ValueError` without `embedding_fn`/`dim`,
  `_memory_lakebase.py:257-262`).
- **Import failure** (`sqlalchemy` / pgvector extras not installed): the stores
  call `_require_sqlalchemy()`; surface as a startup warning and **skip
  attachment** (agent still serves, just without declared memory) rather than
  crashing the whole app — mirrors the dev-UI / MCP best-effort pattern
  (`_wiring.py:140-146`, `:419-427`). Log loudly.
- **Connect failure timing — first-use, not boot.** `auto_create` runs DDL
  **lazily** on first store op (`_memory_lakebase.py:277` flips `_created` on
  first `_ensure_schema`; `_memory_delta.py:293`). So a bad host/instance does
  NOT fail `create_app` — it fails the first `recall`/`remember` mid-turn.
  **Decision required:** add an **eager boot-time validation** (construct the
  engine and run a cheap `SELECT 1` / credential mint at attach time) so
  misconfiguration surfaces at deploy, per the deployment-verification ethos.
  Recommend eager validation gated by a config flag
  (`validate_at_boot: bool = true`) so locked/offline test envs can opt out.
- **`auto_create = false` in locked envs:** when the table is provisioned
  out-of-band and the role can't DDL, set `auto_create = false`
  (supported by all stores). Then a missing table fails at first use with the
  store's own warning (`_memory_lakebase.py:313-318`). Document that
  `ensure_extension = false` is also needed when the Lakebase role lacks
  `CREATE EXTENSION` privilege (`_memory_lakebase.py:222-223`).
- **No principal at runtime:** returns `NO_PRINCIPAL` sentinel, not an error
  (`_memory_tools.py:143-144`) — agents degrade gracefully. Keep this.

## 6. Testing plan, effort, open questions

### Testing plan
- **Config parse:** TOML → `AgentConfig.memory/example/session` for all three
  `type`s; unknown `type` rejected; missing required fields rejected with the
  right message. (unit, no I/O)
- **Attachment + card visibility:** after `setup_agent` runs with a declared
  memory backend, the minted tools appear in `agent._tool_fns`, in
  `agent.collect_tools()`, AND in the **served** `ctx.card` /
  `/.well-known/agent.json` response (the frozen snapshot — assert against the
  served card, not just a fresh `collect_tools()` call, since the snapshot is the
  thing that can go stale). The compiled graph (`compile_to_langgraph`,
  `_compile.py:255`) exposes them as callable StructuredTools. This single test
  guards the §3.1 ordering trap.
- **Dedup precedence:** code-wired `recall` + declared memory → only one
  `recall`, code-wired retained, warning logged.
- **Session override precedence:** `create_app(session_store=X)` + config
  session → `X` used.
- **Isolation (MANDATORY):** two requests with different OBO principals →
  `remember` under user A is NOT recalled by user B; no-principal request →
  `NO_PRINCIPAL`, no leak. Exercises the §4.3 mechanism end-to-end through
  `/invocations`. This is the test that converts §4's *design* into a *verified*
  guarantee.
- **InMemory end-to-end** through `/invocations` (no external deps; CI-safe).
- **Delta / Lakebase:** mock `run_sql` / mock engine for unit; gate any live
  backend test behind an integration marker (FEVM/private-link envs block real
  Lakebase — keep out of default CI).
- **Failure modes:** missing `sqlalchemy` extra → attach skipped, agent serves;
  bad connection + `validate_at_boot=true` → startup error; `auto_create=false`
  + missing table → first-use warning.

### Effort estimate — **M** (~450–650 LOC incl. tests)
- `AgentConfig` fields + 3 nested backend models: **S** (~80 LOC,
  `_models.py`).
- `attach_declared_tools` (inside `setup_agent`) + `resolve_session_store`
  (lifespan) + `_build_store` dispatch + call-site edits (`_wiring.py`
  `setup_agent`, `create_app`): **M** (~150 LOC).
- `LlmAgent.add_tools()` / re-analyze + card sync (`_agents.py`): **S**
  (~40 LOC).
- Embedding-fn builder from endpoint name (`_embeddings.py`, net-new): **S–M**
  (~60 LOC).
- Lakebase engine builder with `do_connect` OAuth listener
  (`_lakebase_engine.py`, net-new): **M** (~80 LOC).
- Principal threading (§4.3 option b: `Dependencies.Principal` + resolver in
  `_make_dep_resolvers` + per-request set in `_compile_run`): **M** (~80 LOC) —
  *this is the riskiest piece; if option (a) contextvar is chosen instead,
  ~40 LOC but with the thread-hop caveat to handle*.
- Tests: ~200 LOC.

Engine size lands at **M** overall; the principal-threading sub-task is the one
that could slip to **L** if the thread-hop interaction (`_compile.py:182-186`)
proves fiddly — recommend prototyping that first.

### Open questions
- **Q1.** Principal threading — §4.3 option (a) contextvar (smaller, thread-hop
  risk) vs (b) injected `Dependencies.Principal` (cleaner, slightly larger).
  Prototype before committing.
- **Q2.** Should `embedding_model` default to a workspace default endpoint, or
  always be explicit? (Lakebase requires it; delta optional.)
- **Q3.** Delta write auth: should config-driven memory writes run as the
  **app SP** (consistent table ownership, simpler grants) or the **per-request
  OBO** principal (tighter audit, but every user needs table write grant)?
  This interacts with §4 isolation — the row key is the principal either way,
  but the *writer identity* differs. Lean SP for writes given the row-level key
  already enforces isolation.
- **Q4.** Should the apps-target path (`mount_mcp_endpoints`) gain real session
  support, or is config session a `create_app`-only feature for now?
- **Q5.** `validate_at_boot` default — true (deploy-time safety) vs false
  (offline/locked test friendliness). Recommend true with documented opt-out.
- **Q6.** Should the existing CLI `memory_store` MODULE:VAR key be deprecated in
  favor of the declarative form, or do they coexist (CLI=code-ref for offline,
  declarative=data for runtime)? Recommend coexist; they serve different
  surfaces.
