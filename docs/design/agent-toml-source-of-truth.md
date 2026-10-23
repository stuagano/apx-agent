# Design: `agent.toml` is the platform's source of truth

**Status:** sub-projects 1–2 implemented (#984); 3a proposed · **Date:** 2026-10-19

## Intent

Settings that Agent Bricks owns live in `agent.toml`. Settings that apx owns
live in Python. Neither side writes the other's file.

Today apx is the source of truth for both. `[tool.apx.agent]` declares stores,
scopes and deploy settings, and apx generates `.build/agent.toml` from it at
deploy (`build_native_manifest`). The Agent Bricks CLI treats `agent.toml` as
"the single source of truth for an agent's resources", so apx re-declares
platform settings that the platform already owns.

Success means:

- a project's authored `agent.toml` is what deploys, byte for byte;
- apx reads that file and checks it against the Python agent, failing loudly
  on a mismatch;
- tools declared in `agent.toml` are callable from an apx agent, under apx's
  per-call governance;
- projects without an `agent.toml` keep working, with a deprecation warning,
  until sub-project 3.

## The four sub-projects

1. **This spec.** `agent.toml` becomes the source of truth; apx reads and
   checks it, and stops generating it.
2. **apx settings on the agent.** Settings `LlmAgent` didn't take yet (model,
   target, gateway, service policies, identity, recovery) are agent
   arguments. The launcher and generated native `start_server.py` compile from
   the agent object when `[tool.apx.agent]` is absent. `[tool.apx.agent]` still
   works until sub-project 3.
3. **Remove `[tool.apx.agent]`**, in four PRs:
   - **3a.** Homes for the last fields (`[tool.apx.deploy]`, `examples=`) and
     `apx-agent agents migrate-config`.
   - **3b.** Every reader and the scaffold/YAML generator use the new homes.
   - **3c.** Migrate the in-repo projects with `migrate-config`.
   - **3d.** `[tool.apx.agent]` is an error naming `migrate-config`, as #944
     did for `[[tool.apx.tools]]`. `template =` and `module =` are removed.
4. **The rest.** A home for Model Serving deploy settings (`responses_agent`),
   and the YAML spec path (#152).

Sub-projects 1 and 2 ship while `[tool.apx.agent]` still works. Nothing breaks
until 3.

## Ownership

| `agent.toml` (Agent Bricks) | Python (apx) |
|---|---|
| `[agent]` framework, server, `deployment_name` | model, instructions, temperature, limits |
| `[[tools]]` and their `downscope` policy | Python tools, `ToolScope` ceilings, per-tool identity |
| `memory_store`, `session_store` | `sub_agents`, bindings, routing |
| tracing experiment | guardrails, gateway, service policies |
| `[auth.user]` `required`, `additional_api_scopes` | agent identity, recovery opt-in |

Space and instance count are `agentbricks deploy` flags, not `agent.toml`
fields, so they stay on the apx side. Non-managed memory and session backends
(Lakebase, Delta, in-memory) are apx's and stay in Python.

## Data flow

```text
agent.toml (written by the user or the agentbricks CLI)
        │  read with the SDK's AgentProject.load; never written by apx
        ▼
check_agent_toml ── compares with the Python agent ── findings
        ▲
agent.py
  Agent(model=…, tools=[run_sql, *platform_tools()], …)
```

apx reads the file with `databricks_agentbricks.projects.agent_project.AgentProject.load`,
the platform's own parser. apx has no `agent.toml` parser of its own.

## `platform_tools()`

```python
from apx_agent import Agent, platform_tools

triage = Agent(name="triage", tools=[run_sql, *platform_tools()])
pricing = Agent(name="pricing", tools=[quote, *platform_tools("genie-pricing")])
```

- `platform_tools(*ids)` returns placeholders and does no network or auth work,
  so `agent.py` stays importable anywhere. No ids means every `agent.toml`
  tool; ids select specific ones, so a composite can give different tools to
  different leaves.
- Placeholders resolve through AgentKit's loaders. `genie_tools()` covers
  `genie_agent`; `mcp_tools()` covers `mcp`, `uc_function`, `genie_one` and
  `sandbox`. apx adds no MCP or Genie wiring of its own.
- Agent tools that use `platform_tools()` with no `agent.toml` (or no project root) are a compile error, not a `/readyz` degradation.
- **Identity:**
  - `genie_agent` tools are built once at startup. `GenieAgent._client()` calls its
    `workspace_client_for` resolver on every call, so apx passes a resolver
    that reads the current request's `RequestAuthContext.client_for` (the
    seam #902 uses).
  - `auth = "user"` MCP-backed tools (`mcp`, `uc_function`, `genie_one`,
    `sandbox`) are fetched **per invocation**, inside the
    request. `_server_from_tool` resolves the workspace client when the
    server is built, so a startup fetch would bind every caller to one
    identity.
  - `auth = "app"` MCP tools are fetched once at startup with the service
    client.
  - A user tool never falls back to the app identity. In deployed Apps the SDK
    raises `AuthError` (`MCP_USER_AUTHORIZATION_MISSING`) without a request
    resolver, and apx lets it surface.
- **Governance:** materialized tools join the agent's tool list under their
  real names. apx's per-call guards (approvals, budget, watchdog, audit spans)
  run on them as on Python tools. The data ceiling is the platform's
  `downscope`; a `ToolScope` on a platform tool is a compile error, so one
  tool's data limits are never declared in two places.
- A Genie binding yields three tools: `<id>_ask`, `<id>_poll` and
  `<id>_query_result`. The collision check uses these names.

## Checks

One function, `check_agent_toml(project, agent, authorization_plan)`, returns
findings in doctor's `Check` shape (status, detail, fix). Every caller uses it.

| Caller | Findings | Effect |
|---|---|---|
| `compile_agent` and launcher startup | runtime-breaking only: unknown `platform_tools()` id, `ToolScope` on a platform tool, name collision | compile error |
| `agents deploy` preflight (Agent Bricks CLI and `apps_api` backends) | all | fails before staging or deploy |
| `apx-agent doctor`, `doctor --durable` | all | offline report with fixes |

The checks:

1. **Loads.** `AgentProject.load` succeeds, `framework = "langgraph"`, and
   `server` matches the compile target.
2. **User auth.** The plan needs a request user when an operation requires
   request context, the plan has app dependencies, memory is declared, or a
   referenced `agent.toml` tool has `auth = "user"` (the same rule the
   generated manifest and migration use). Then `[auth.user]` must set
   `required = true` and list every plan scope in `additional_api_scopes`.
   Otherwise scopes are not checked; the SDK rejects scopes unless
   `required = true`.
3. **Stores.** Managed memory or managed sessions in Python need a bound
   `memory_store` / `session_store`. While `[tool.apx.agent]` exists, a store
   name it declares must equal the `agent.toml` binding.
4. **Tools.** Every `platform_tools()` id is declared, every declared tool is
   referenced by some agent, and no names collide.
5. **Tracing.** Not checked: `AgentConfig` has no tracing field, so there is nothing to compare.

Managed-store provisioning (an idempotent get-or-create of the declared stores) may already have run when the preflight fails.

Every mismatch is an error. The only warning is a missing `agent.toml` during
the transition.

## Staging and deploy

- With an authored `agent.toml` at the project root, deploy copies it into
  `.build/` unchanged. The `apps_api` path's refusal of a root `agent.toml`
  (`_apps_api_deploy._stage`) is reversed. Symlinked manifests stay refused.
- Without one, deploy generates `.build/agent.toml` exactly as today and logs a
  deprecation warning that names `apx-agent agents migrate-agent-toml`.

## Migration command

`apx-agent agents migrate-agent-toml` writes the authored `agent.toml` from the
current `[tool.apx.agent]` and authorization plan. It makes one SDK call,
`AgentProject.create(project_root, framework="langgraph", server="agentbricks",
memory_store=..., session_store=...).write()`, then appends `[auth.user]` to the
written file as text (no `AgentProject` method sets it), then reloads the file
with `AgentProject.load` as a read-after-write check. This command is the only time apx causes `agent.toml` to be
written. It refuses to overwrite an existing `agent.toml`. The migrated and checked session store follows `inspect_target(...).session_store` (composite agents bind none), matching the generated manifest.

## Testing

**Unit (in `make check`):**

- Each check has a test with a mismatched `agent.toml` asserting the exact fix
  text, and a clean counterpart with zero findings.
- Without `agent.toml`, the generated `.build/agent.toml` is byte-identical to
  today's output and a deprecation warning is logged. With one, the staged
  file is byte-identical to the authored file and nothing is generated.
- `platform_tools()`: unknown id, unreferenced tool, collision, and `ToolScope`
  on a platform tool each fail. An `auth = "user"` resolver returns the
  request-user client and raises without a request context.
- An approval-gated, budgeted agent gates and counts platform tool calls the
  same as Python tool calls.

**Reality (`*_reality_ctk.py`):**

- `agents migrate-agent-toml` on a copy of a real example: read the result back with
  `AgentProject.load` and assert stores, scopes and tools round-trip.
- Deploy staging on a real example: `ctk.verify(Artifact(".build/agent.toml",
  must_contain=...))` matches the authored file.

**Capabilities (`capabilities.yaml`):**

- `agent-toml-mismatch-fails-preflight` (cheap): a scope missing from
  `agent.toml` fails deploy before any workspace change.
- `platform-tool-runs-under-apx-guards-live` (live, fevm): an `agent.toml`
  Genie tool called through `platform_tools()` on a deployed durable agent
  carries apx audit attributes and runs as the request user.

## Sub-project 2: settings on the agent

#974 put `model`, `target`, `identity`, `recovery`, `gateway` and
`service_policies` on `LlmAgent`, read by `config_from_agent` only when
`[tool.apx.agent]` is absent. #961 and #974 together fixed the three #954 gaps
(Python-only memory reaches the `agent.toml` checks, and deploy runs them).
Remaining:

- **One seam.** `resolve_settings(agent, toml)` replaces `config_from_agent`
  in the launcher, generated `app.py`, deploy, doctor and `finalize_agent`.
- **Both sources merge.** Equal values pass; different values fail:
  `model: agent.py sets 'A' on agent 'root'; [tool.apx.agent] sets 'B'. Set it in one place.`
  Today Python is silently ignored, except `identity`, where it silently wins.
- **Every agent class takes the settings.** Unknown setting names are a
  `TypeError`.
  - Per-agent (`model`, `identity`): own value, else nearest ancestor, else the
    app value. A leaf compiles with its own model; its tools get its own
    identity (today only the root's tools are stamped). `APX_MODEL` pins the
    app model; a leaf with a different model of its own keeps it.
  - App-wide (`target`, `recovery`, `gateway`, `service_policies`,
    `guardrails`, `knowledge`, `vector_search_index`, `bindings`, `url`,
    `registry`, `api_prefix`): a value below the root that differs fails:
    `target is app-wide; set it on the root agent (found on 'researcher').`
    Their consumers read one value per app today; making any of them per-agent
    is a later change, never a silent ignore.
  - `memory` / `session` keep #974's rule: read from the root's `memory=` knob.
- **Fails early.** Deploy before upload, doctor as a stage, the launcher at
  startup (its existing target error names both places).
- **Migration.** `.toml`-only projects are unchanged. Fields move one at a
  time; a field set in both places with the same value passes. No migrate
  command. The scaffold moves with sub-project 3, when `[tool.apx.agent]` goes.
- **Tests.** `resolve_settings` rules and errors; per-leaf model and identity;
  a composite Python-only project loads in doctor and the launcher; the
  existing suite unchanged.

## Sub-project 3a: homes for the last fields

After #984, runtime settings live on the agent. A few things still have no
home outside `[tool.apx.agent]`:

- deploy settings;
- `host`;
- the app `name`, which deploy needs before it imports `agent.py`;
- dev-UI `examples`.

`data`, `executor`, `workflows` and `example` are YAML-spec inputs only. No
pyproject sets them, and the generator compiles them to Python.

### `[tool.apx.deploy]`

```toml
[tool.apx.deploy]
name = "orders-agent"     # optional; defaults to [project].name
host = "apps"             # apps | model_serving
space = "shared-space"    # an App Space picks compute; without one:
# instances = 2           #   fixed count, or [tool.apx.deploy.autoscale] min/max, 1-5
backend = "agentbricks"   # | apps_api (+ entrypoint, include, env)
```

- **Shape and validation.** The table holds `DeployConfig`'s fields at the top
  level, plus `name` and `host`. Validation is `DeployConfig`'s:
  - `instances` XOR `autoscale`;
  - `entrypoint` is required for `apps_api`;
  - `include` paths must stay inside the project.
- **`name` default.** `[project].name`, but only when neither table sets
  `name`. A default never conflicts with a declared value.
- **`app_name`** (#986) already lives here. It names the Apps API App, and
  `_apps_api_app_name` reads it, so the fold leaves it alone.
- **No import.** Deploy reads the table without importing `agent.py`, so its
  prerequisite checks still run before import.
- **One seam.** `_load_agent_config` folds the table into the `AgentConfig` it
  returns, as `name`, `host` and `deploy`. Readers of `config.deploy` and
  `config.name` don't change.
  - With only `[tool.apx.deploy]`, the config is built from that table, and
    `resolve_settings` adds the Python settings.
  - With neither table, it returns `None`, as today.
- **Conflicts.** A field can sit in both `[tool.apx.agent]` (or its `.deploy`
  sub-table) and `[tool.apx.deploy]`. Equal values pass. Different values fail:
  `deploy.space is declared in both [tool.apx.agent.deploy] and [tool.apx.deploy] with different values; keep [tool.apx.deploy].`

### `examples=`

`examples` becomes an app-wide agent setting (`list[str]`) in `AgentSettings`.
It merges through `resolve_settings`, so the dev UI's reads of
`config.examples` don't change.

### `apx-agent agents migrate-config`

The command is safe to run repeatedly. Each run:

1. Moves `name`, `host` and `[tool.apx.agent.deploy]` into `[tool.apx.deploy]`.
2. Prints the keyword arguments for each runtime field the agent doesn't
   declare yet, for example `Agent(..., model="…", examples=["…"])`. It never
   edits `agent.py`.
3. Imports the agent and runs `resolve_settings`. If every remaining
   `[tool.apx.agent]` field is declared in Python with an equal value, it
   deletes the table. Otherwise it keeps the table and lists what's missing.
4. Re-parses the file with `tomllib`, reloads the config, and requires the
   effective config to be equal before and after. If it differs, the command
   restores the file and fails.

It edits the file as text, cutting the `[tool.apx.agent*]` sections and
appending `[tool.apx.deploy]` with `_project_gen`'s `_toml_value`, so there's
no new dependency. It refuses to run on a `pyproject.toml` with uncommitted
changes, so git is the undo.

These are errors:

- `module =` other than `agent:agent`: rename the module or file.
- `template =`: generate an `agent.py` from the template first.
- `data`, `executor`, `workflows` or `example`: move them to the YAML spec.

### Testing

- **Loader:**
  - `[tool.apx.deploy]` alone gives `name`, `host` and `deploy`, with `name`
    defaulting to `[project].name`.
  - With both tables, equal values pass and different values fail with the
    message above.
  - With neither table, it returns `None`.
  - `DeployConfig` validation fires from the new table.
- **Deploy before import.** A deploy with only `[tool.apx.deploy]` reads
  `space` and `backend`, and fails its prerequisite checks without importing
  `agent.py`. This is a variant of
  `test_agent_prerequisites_block_deployment_before_upload`.
- **`examples=`** round-trips through `resolve_settings`, shows on the dev-UI
  landing page, and fails as app-wide when set on a child.
- **`migrate-config`**, run on a copy of `data-inspector`:
  - Run 1 writes `[tool.apx.deploy]`, prints the snippet and keeps the table.
  - Run 2, after the snippet is pasted into `agent.py`, removes the table.
  - Each run is read back with
    `ctk.verify(Artifact(..., must_contain="[tool.apx.deploy]"))` and
    `tomllib`, and the effective config is equal before and after.
  - It refuses a dirty file, `module =` other than `agent:agent`, and
    `template =`.
  - A forced read-back mismatch restores the file.
- **Gate:** `make check` and pre-commit on the changed files. The existing
  suite passes unchanged.

## Out of scope

- Switching readers off `[tool.apx.agent]`, migrating the in-repo projects, and
  rejecting the table (3b–3d).
- Model Serving deploy settings and the YAML spec path (sub-project 4).
- Tool kinds added to `agent.toml` later that AgentKit's LangGraph loaders don't
  cover. `platform_tools()` rejects an unknown kind at compile time rather than
  skipping it.
