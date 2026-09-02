# Design: AppKit as APX-internal Apps host

**Status:** proposed - **Date:** 2026-08-27 - **Scope:** Databricks Apps deploy target

APX's external authoring surface stays Python. Users declare agents, tools,
resources, memory, policies, and deploy intent through `apx_agent` and
`apx-agent agents *`. AppKit is allowed inside the Apps deploy implementation,
but must not become an APX product interface or a second SDK users import.
AppKit remains internal even after it becomes the Apps implementation; there is
no future state where APX users choose or author against an AppKit adapter.

The goal is not "APX users write AppKit." The goal is:

```text
Python APX declaration
-> APX compiler/deploy target
-> generated internal AppKit host
-> Databricks Apps runtime
```

This keeps APX deep at its declaration interface while replacing only the Apps
host implementation under that interface.

## Current state

Today, a scaffolded Apps project is Python-native:

1. User edits top-level `agent.py`.
2. Generated `agent_server/start_server.py` imports `agent`.
3. The server loads `[tool.apx.agent]`, calls `finalize_agent(...)`, resolves the
   configured conversation store, and compiles with `compile_to_responses_agent`.
4. The module registers MLflow `agent_server` `@invoke` / `@stream` handlers.
5. The same FastAPI app mounts MCP, A2A discovery, `/readyz`, and the APX dev UI.

The AppKit spike added a private TypeScript runtime path:

- `typescript/package.json` is now `apx-internal-runtime` and `"private": true`.
- `typescript/src/internal/appkit-host.ts` contains the AppKit host module.
- Public TypeScript exports intentionally do not expose AppKit host symbols.
- Generated TypeScript scaffolds depend on `"apx-internal-runtime": "file:.."`.
- The generated AppKit host starts a bridge-only Python FastAPI app at
  `agent_server.appkit_bridge:app`; the old `agent_server.start_server:app`
  remains the Python fallback host, not the AppKit-started Apps runtime.

The current missing pieces are parity hardening and default cutover.

## Non-goals

- No public TypeScript APX SDK.
- No requirement that agent authors write AppKit, TypeScript, or generated host
  files by hand.
- No replacement of Model Serving in this increment.
- No deletion of the current Python Apps runtime until parity gates pass.
- No new user-facing deploy target flag beyond a temporary migration gate.
- No live Databricks deployment behavior change without a rollback path.

## Interface decision

The external module interface remains:

```python
from apx_agent import LlmAgent, tool

@tool
def lookup_policy(resource: str, ws: Dependencies.Workspace) -> dict:
    ...

agent = LlmAgent(
    name="pricing_advisor",
    model="databricks-claude-sonnet-4-6",
    instructions="...",
    tools=[lookup_policy],
)
```

The Apps deploy target may compile that declaration to either host internally:

| Host | Role | Visibility |
|---|---|---|
| Python ResponsesAgent host | Current fallback / parity baseline | generated boilerplate |
| AppKit host | Target Apps implementation | generated/internal only |

The seam is **APX Apps host compilation**, not a new authoring interface.

## Proposed architecture

```text
agent.py / pyproject.toml
        |
        v
Python finalization
  - apply template/config knobs
  - resolve memory/session config
  - derive resources and OBO scopes
  - inspect tools and schemas
        |
        v
APX Apps host manifest
  - agent metadata
  - model and generation settings
  - tool definitions and strict schemas
  - tool annotations and approval posture
  - resource hints / OAuth scopes
  - governance hooks enabled for runtime
        |
        v
Generated Apps host
  - TypeScript AppKit server
  - apx-internal-runtime dependency
  - local Python tool bridge when needed
  - AppKit agent definition and toolkit
        |
        v
Databricks Apps
  - AppKit routing / streaming / HITL
  - APX policy, audit, trace, OBO semantics
```

### 1. Python host manifest

Add a Python internal module that emits a stable data artifact:

```python
compile_to_apps_host_manifest(agent, config, *, target="appkit") -> AppsHostManifest
```

This is not a new public API. It is deploy/scaffold machinery used by
`apx-agent agents scaffold` and `apx-agent agents deploy`.

The manifest should contain only portable facts:

- `agent.name`
- `agent.instructions`
- `agent.model`
- generation settings (`max_iterations`, max tokens, temperature when allowed)
- tools with name, description, JSON schema, runtime kind
- tool annotations: read/update, requires user context, approval posture
- declared resources and derived Apps OAuth scopes
- session/memory config references
- discovery metadata needed for A2A/MCP/topology
- trace/audit configuration

It should not contain Python callables, instantiated clients, secrets, or user
tokens. Those are runtime concerns.

### 2. Tool execution bridge

Tools split into two categories:

| Tool kind | Execution path |
|---|---|
| Platform-native / UC-backed / managed resource tools | Prefer direct AppKit/platform execution when the APX semantics match. |
| Arbitrary Python tools | Execute through a same-container APX Python tool bridge. |

The Python bridge is the conservative first implementation. It avoids forcing
all user tools to become TypeScript and preserves current dependency injection
semantics for `Dependencies.Workspace`, `Dependencies.Sql`, `Dependencies.State`,
progress emission, and principal resolution.

The bridge must be local to the deployed App container. It must not expose a
new external tool API. The AppKit host calls it as implementation detail.

### 3. Governance wrapping

The AppKit host must not bypass APX governance. Every tool call goes through:

```text
AppKit tool call
-> APX tool annotations / approval posture
-> APX policy decision
-> APX audit start/allow/deny/error event
-> actual tool execution
-> trace/audit finalization
-> AppKit result
```

Minimum preserved semantics:

- policy deny short-circuits before tool execution
- audit records allow, deny, and execution error
- OBO context is available to user-scoped tools
- mutating tools are visible to AppKit approval/HITL semantics
- errors preserve enough context for APX diagnostics without leaking secrets

### 4. AppKit ownership map

AppKit should own generic Apps runtime mechanics. APX should own the declared
governance layer and the compiler from Python declarations into that runtime.

| Runtime surface | Owner in AppKit-hosted Apps | APX responsibility |
|---|---|---|
| HTTP server, `/health`, static serving, dev server lifecycle | AppKit `server()` | Generate the host and deployment config. |
| `/invocations`, `/responses`, `/chat`, streaming transport | AppKit `agents()` | Preserve request/response parity through compiled declarations. |
| Thread runtime, cancellation, stream ownership, runtime limits | AppKit `agents()` | Compile APX knobs into AppKit config; `max_iterations` currently feeds AppKit `maxToolCalls`. |
| OBO request context | AppKit execution context and `asUser(req)` | Forward the scoped context through the local Python bridge while Python tools remain external. |
| HITL execution mechanics | AppKit approval flow | Compile APX tool annotations/policy into approval posture and audit the decision. |
| Generic Databricks SQL/File/Genie/Jobs/Serving resource calls | AppKit plugins where equivalent | Keep APX domain wrappers only when they add governance or semantics. |
| Python tool execution | APX bridge | Keep this local, internal, and minimal: execute the named APX tool under the governed context. |
| APX policy, audit, provenance, trace projection | APX | Never let a tool execute outside this wrapper. |

### 5. Generated host layout

The Apps scaffold should continue showing `agent.py` as the user-owned file.
Generated AppKit internals should live under generated/build paths, for example:

```text
agent.py                         # user-owned
pyproject.toml                   # user-owned config
databricks.yml                   # generated but editable deployment config
agent_server/                    # generated framework files
.build/apx_appkit_host/          # deploy-staged generated TypeScript host
.build/agent_server/appkit_bridge.py
```

The exact path can change, but the ownership rule is fixed:

- user edits `agent.py`, `pyproject.toml`, and deployment config
- APX owns generated AppKit host files
- deploy rebuilds generated host files from source declarations

### 6. Internal host cutover

The Python host remains the default until the parity gates below pass. AppKit is
an explicit opt-in while the generated host is still being hardened:

```text
APX_APPS_HOST=python   # default governed Python host
APX_APPS_HOST=appkit   # explicit generated AppKit host opt-in
```

After parity gates pass, the same public deployment command stages the AppKit
implementation by default:

```text
apx-agent agents deploy --target apps
-> generated internal AppKit host
```

The gate is internal/experimental. It is a migration guard, not a supported
product mode or public adapter choice.

## Parity gates

AppKit cannot become the internal Apps implementation until all gates pass
against the same Python declaration.

### Identity / OBO

- Apps proxy `X-Forwarded-Access-Token` reaches tool execution.
- `Dependencies.Workspace` calls run as the caller, not the App service principal.
- Missing OBO in multi-user Apps fails closed where the Python host fails closed.
- Reauthorization hints remain visible for missing scopes.

### Tooling

- Plain sync and async Python tools execute correctly.
- Dependency-hidden parameters stay hidden from model-visible schemas.
- Strict JSON schemas match current APX tool schemas.
- Stateful tools using `Dependencies.State` retain current semantics.
- Tool progress can still emit trace progress events.

### Governance

- Policy allow / deny behavior matches Python host.
- Mutating tool annotations drive AppKit approval/HITL posture.
- Audit events include tool name, action, reason, error, and non-sensitive input
  shape/hash as applicable.
- No governance hook can be skipped by calling the bridge directly.

### Runtime contract

- `/invocations` accepts the same request shape expected by deployed Apps users.
- Streaming output preserves currently supported event semantics.
- Multi-turn `thread_id` / `session_id` behavior matches the current Apps host.
- Conversation history persists through the configured APX conversation store.

### APX surfaces

- `/.well-known/agent.json` remains available.
- `/mcp` remains available for Apps-hosted custom tools.
- `/_apx/*` dev UI remains available or has a deliberate replacement.
- `/readyz` remains the deploy gate and proves a real agent turn plus trace path.
- Topology/discovery metadata remains equivalent.

### Deploy/build

- `apx-agent agents deploy --target apps` still owns wheel/runtime staging.
- `databricks.yml` resource auto-update and `user_api_scopes` derivation still run.
- Generated host build is deterministic from Python source and config.
- The deployed bundle does not rely on symlinks that cannot resolve in Apps.
- Lockfiles and registry URLs remain sanitized by existing checks.

## Deletion tests

Do not keep both hosts indefinitely. The AppKit path becomes the internal Apps
implementation only when these deletion tests pass:

1. Delete the public AppKit adapter export surface: already done.
2. Delete public `appkit-agent` package identity: already done.
3. Replace Apps scaffold runtime generation without changing user-authored
   `agent.py`.
4. Run the same Apps smoke fixture against Python host and AppKit host; compare
   identity, trace, audit, tool results, session persistence, MCP, A2A card, and
   readyz output.
5. Cut the internal Apps implementation to AppKit behind a fallback gate.
6. After at least one release window with fallback unused for known-good
   fixtures, delete the Python Apps host scaffold path or demote it to legacy
   migration code with an explicit removal issue.

## Implementation phases

### Phase 0 - Spec and guardrails

- Keep `apx-internal-runtime` private.
- Keep AppKit host symbols out of TypeScript public exports.
- Add/keep tests that fail if AppKit host names leak through `src/index.ts`.
- Document this design.

### Phase 1 - Manifest compiler

- Add `AppsHostManifest` models in Python.
- Emit a manifest from a finalized `BaseAgent`.
- Unit-test model/instructions/tool schema/resource/scope extraction.
- No deploy behavior change.

### Phase 2 - Generated AppKit host skeleton

- Generate TypeScript host files into `.build/apx_appkit_host/`.
- Build the generated host with `apx-internal-runtime`.
- Prove the generated host can expose a trivial read-only tool.
- Keep the Python host as the Apps default; allow `APX_APPS_HOST=appkit` as an
  explicit opt-in while parity work continues.

### Phase 3 - AppKit route ownership

- Keep AppKit `server()` / `agents()` as the only owner of Apps-facing routes
  in the AppKit-started path.
- Start a bridge-only Python app at `agent_server.appkit_bridge:app`.
- Keep `agent_server.start_server:app` as the Python fallback host only.
- Add regression tests that the AppKit-started bridge does not compile the
  ResponsesAgent host or mount MCP/readyz/dev UI routes.

### Phase 4 - OBO, HITL, limits, and surfaces parity

- Preserve dependency injection and OBO resolution through AppKit `asUser(req)`.
- Route every Python tool call through policy/audit wrappers.
- Compile APX tool annotations into AppKit approval posture.
- Compile APX `max_iterations` into AppKit `limits.maxToolCalls`.
- Mount or proxy MCP, A2A discovery, dev UI, topology, and readyz only after
  deciding whether those remain APX surfaces or become AppKit plugin surfaces.
- Compare outputs against the current Python host on the same declaration.
- Add a parity fixture to CI that runs both hosts locally where possible.

### Phase 5 - Internal Apps cutover

- Make generated AppKit internals the implementation behind
  `apx-agent agents deploy --target apps`.
- Keep Python host fallback for one release window.
- Document fallback only as migration/debug escape hatch, not as a public
  adapter choice.

### Phase 6 - Remove legacy host

- Delete or quarantine the Python Apps host path after parity plus release-window
  evidence.
- Remove fallback env/config.
- Keep Model Serving unchanged.

## Risks

| Risk | Mitigation |
|---|---|
| Two runtimes diverge | Short fallback window; explicit deletion tests. |
| OBO behavior regresses | Identity parity tests with user-scoped WorkspaceClient calls. |
| Python bridge becomes public API | Bind only to generated host/local container; no docs, no external route contract. |
| Generated TS host adds deploy flake | Build generated host during deploy plan/dry-run before bundle deploy. |
| AppKit approval semantics do not match APX policy | Treat AppKit HITL as presentation/execution control, APX policy as authoritative gate. |
| Dev UI/MCP/A2A surfaces lag | Phase 4 blocks internal cutover. |

## Open questions

1. Should the first AppKit host mount APX's existing FastAPI surfaces by running
   a sidecar Python server, or should TypeScript proxy only the required APX
   routes directly to Python bridge handlers?
2. Which tool kinds can bypass the Python bridge safely because they are already
   platform-native without losing APX audit/policy wrapping?
3. Should the host manifest be written to `.build/` only, or also exposed as a
   debug artifact under `.apx/` for inspection?
4. What is the smallest local parity harness that exercises streaming,
   conversation persistence, OBO, audit, and readyz without requiring a live
   Databricks Apps deployment?
