# Design: `agent.toml` is the platform's source of truth

**Status:** implemented (sub-project 1) · **Date:** 2026-10-19 · **Sub-project:** 1 of 4

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
2. **apx settings on the agent.** Settings `LlmAgent` doesn't take yet (model,
   target, gateway, service policies, identity, recovery) become agent
   arguments. The launcher and generated `app.py` compile from the agent
   object instead of `pyproject.toml`.
3. **Remove `[tool.apx.agent]`.** Fail with migration guidance (as #944 did for
   `[[tool.apx.tools]]`), and migrate the examples, scaffold, YAML generation
   and doctor.
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

## Out of scope

- Removing `[tool.apx.agent]` (sub-project 3).
- Moving model, identity and other apx settings onto the agent (sub-project 2).
- Model Serving deploy settings and the YAML spec path (sub-project 4).
- Tool kinds added to `agent.toml` later that AgentKit's LangGraph loaders don't
  cover. `platform_tools()` rejects an unknown kind at compile time rather than
  skipping it.
