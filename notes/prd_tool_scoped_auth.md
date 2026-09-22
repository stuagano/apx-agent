# PRD: Tool-Scoped Auth — per-tool identity + permission ceiling, declared

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-11 | **Slug**: `tool_scoped_auth`

## Summary
Let each tool in an apx-agent declaration bind **its own** execution identity
(`sp` | `obo` | a named narrow service principal) **and** a permission-scope
ceiling (which UC catalogs/schemas/tables/functions it may touch, which secret
scopes it may read), enforced at **compile time** (deploy blocked on an invalid
or over-scoped declaration) and at **runtime** (out-of-scope call refused with a
new `scope_denied` error that is logged + traced). Today the whole agent shares
one execution identity (`_obo.py` OBO or the App service principal) and tools run
under broad UC governance; this narrows authority to the individual tool,
declaratively — "declared, not wired." The seams already exist: `build_tool`
already takes `execution: ExecutionIdentity` (`_tool_factory.py:49`) and UC
objects are already declared as `ResourceSpec`/`_apx_resources`
(`_resources.py:69`); this PRD adds the *scope ceiling* and its two enforcement
points on top of those, plus back-compat (undeclared = today's unrestricted
behavior). **Primary success metric: failing gate-test count → 0**
(`cd python && uv run pytest -k scoped_auth` green + `make check` green).

## Background
**What exists today (all reusable):**
- **Declaration parser.** `[[tool.apx.tools]]` tables are parsed in
  `python/src/apx_agent/_tool_config.py` — `_read_tools_section` reads the TOML,
  `_build_one` pops `type`, looks the factory up in `_registry()`, and splats the
  remaining keys as kwargs; `load_config_tools`/`merge_config_tools` build and
  attach the callables. `ToolConfigError(ValueError)` is the config-validation
  error already raised for a bad table. **This is exactly where per-tool
  `identity` / `scope` / `secret_scopes` keys parse** — pop them before the
  factory splat, like `type`.
- **Identity seam.** `build_tool(call, *, name, description, resources=(),
  execution: ExecutionIdentity | None = None)` (`_tool_factory.py:43`) already
  stamps a per-tool `ToolMetadata(execution=...)` onto `call._apx_tool`.
  `_apps_authorization.py` resolves the runtime credential:
  `infer_operation_authorization(fn)` → `OperationAuthorization(execution_identity,
  user_api_scopes, ...)` where `execution_identity` is `"user"` (OBO) or
  `"service"` (SP), and an **explicit vs inferred conflict already raises**
  (`_apps_authorization.py:293`) — the exact compile-time hard-fail pattern to
  reuse. OBO plumbing itself lives in `_obo.py` (`extract_obo_headers`,
  `make_obo_workspace_client`, `resolve_no_obo_or_raise` → `ApxIdentityError`,
  `_sp_fallback_allowed()` gated by `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK`) and
  the per-request `UserClientDependency` in `_defaults.py`.
- **Runtime governance gate.** `before_tool` guards (`_guards.py` — `RateLimit`,
  `ToolAllowlist`, `WatchdogGuard.for_tool()`) raise; `_watchdog.py` stamps the
  decision on the active span. `_compile._governance_exception_middleware()`
  (`_compile.py:295`) contains `_CONTAINED = (PermissionError, ToolCancelled,
  ToolError)` into an error `ToolMessage` so a denial keeps the turn alive
  instead of a 500. `ApxIdentityError` and `ApprovalRequired` already subclass
  `PermissionError` to ride that containment.
- **Audit/trace.** `_audit.py` — `AuditAttrs` holds the stable `apx.*` span-key
  schema (note the existing `WATCHDOG_ACTION`/`WATCHDOG_REASON` pair); write via
  `set_audit_attrs(span, ...)` (unknown kwargs raise — fail-loud), read the active
  span via `current_active_span()`. This is the audit-event emission point.
- **Error contract.** `_errors.py` `ToolError(Exception)` is the **tools-route
  error** contract (denied query / missing table → contained `ToolMessage`). The
  new `scope_denied` must be a **distinct** type (not `ToolError`).

**What's missing.** There is no *scope ceiling*: nothing caps which UC objects a
tool may touch beyond broad UC grants, nothing lets a single tool declare "run
OBO, read only `catalog X`", and there is no compile-time over-scope check or
runtime `scope_denied` refusal. Everything above is reused; this PRD adds one
scope module, two enforcement hooks, audit keys, and tests.

## Research Inputs
- `_tool_config.py` — `_build_one`/`_registry`/`load_config_tools` (parse point);
  `ToolConfigError` (config-error idiom).
- `_tool_factory.py:43` `build_tool(execution=...)`; `_resources.py:69`
  `ResourceSpec(kind, identifier)` with kinds incl. `uc_function`, `uc_table`;
  `attach_resources`/`get_resources` (`_apx_resources`) — the UC-object seam.
- `_apps_authorization.py` — `ExecutionIdentity` (`"user"`/`"service"`),
  `infer_operation_authorization`, explicit-vs-inferred conflict raise.
- `_obo.py` — OBO extraction, SP-fallback gate, `ApxIdentityError(PermissionError)`.
- `_compile.py:295` `_governance_exception_middleware` + `_CONTAINED`.
- `_guards.py` — `before_tool` guard pattern (`WatchdogGuard.for_tool()`).
- `_audit.py` — `AuditAttrs`, `set_audit_attrs`, `current_active_span`.
- `_errors.py` — `ToolError` (the contract the new error must NOT reuse).
- Test layout: `python/tests/test_*.py`, reality checks `*_reality_ctk.py`,
  capability manifest `python/capabilities.yaml` (cheap/live tier + `check:` path);
  ctk `Artifact`/`verify` on the pytest `pythonpath`.

## Goals
- A tool declares **identity mode** (`sp` | `obo` | `named-SP:<name>`),
  **UC-object scope** (catalogs / schemas / tables / functions), and a
  **secret-scope allowlist** in its `[[tool.apx.tools]]` table (or Python
  `build_tool(...)` call).
- **Compile time:** hard-fail the build/deploy when the declaration is invalid
  (unknown key, malformed scope, identity conflict) or the tool is **over-scoped**
  (a declared `ResourceSpec` outside its declared scope ceiling).
- **Runtime:** refuse an out-of-scope call with a distinct `scope_denied` error,
  contained (not a 500), and emit an audit event (logged + traced on the span).
- **Back-compat:** a tool with **no** scope declared keeps today's unrestricted
  behavior — opt-in, purely additive.
- **Framework-transparent (NFR):** good defaults, clear error messages, no
  hand-wiring of credentials or grants.

## Non-Goals (v1)
- External-host / outbound-network allowlist (deferred; `APX_TOOLS_ALLOWED_HOSTS`
  in `_tool_config._check_allowlist` stays the only host gate).
- **Deny-by-default** — an undeclared scope stays unrestricted for back-compat.
- Per-hop A2A **re-scoping** beyond what identity pass-through already does
  (`_obo.py` / `_a2a.py`).
- Auto-provisioning UC grants (OKF→UC writes remain a governed tool, not this).

## Requirements
### Functional
- **FR-1 — Per-tool declaration.** A `[[tool.apx.tools]]` table may carry optional
  keys `identity` (`"sp"`|`"obo"`|`"named-sp:<name>"`), `scope` (a table with
  optional `catalogs`/`schemas`/`tables`/`functions` string lists), and
  `secret_scopes` (string list). New module
  `python/src/apx_agent/_tool_scope.py` defines a frozen `ToolScope` dataclass +
  `parse_tool_scope(table) -> ToolScope | None`. `_tool_config._build_one` pops
  these keys (like `type`) before the factory splat and calls `attach_scope(fn,
  scope)` (mirrors `attach_resources` → new `_apx_scope` attribute); `get_scope(fn)`
  reads it back. Python authors pass the same via `build_tool(...)` — reuse the
  existing `execution=` arg for identity, add a `scope=`/`secret_scopes=` pass-through.
- **FR-2 — Identity mapping.** `obo` → `ExecutionIdentity` `"user"`, `sp` →
  `"service"`, `named-sp:<name>` → `"service"` tagged with the SP name on
  `ToolMetadata`. Reuse `build_tool`'s existing `execution` stamping and
  `_apps_authorization.infer_operation_authorization`; a declared identity that
  conflicts with the tool's inferred identity reuses the existing conflict-raise.
- **FR-3 — Compile-time enforcement.** `load_config_tools` (already the
  config-validation surface) calls a new `validate_tool_scope(fn)` after building
  each tool: raise `ToolConfigError` when the declaration is malformed, and raise
  when any `ResourceSpec` on `_apx_resources` (a `uc_function`/`uc_table`
  identifier) falls **outside** the declared `scope` ceiling (over-scoped). A
  deploy preflight in the compile/deploy path (`_compile_run.py` / the
  `agents deploy` CLI) runs the same validator over every tool so an over-scoped
  tool **blocks deploy with a non-zero exit + a clear message**, never ships.
- **FR-4 — Runtime enforcement + `scope_denied`.** New
  `ScopeDenied(PermissionError)` in `_tool_scope.py` (subclasses `PermissionError`
  so `_governance_exception_middleware`'s `_CONTAINED` already contains it into an
  error `ToolMessage` — **distinct type from `ToolError`**, message prefixed
  `scope_denied:`). A `before_tool` guard `ScopeGuard.for_tool()` (sibling to
  `WatchdogGuard.for_tool()` in `_guards.py`) reads `get_scope(fn)`; when a call's
  target UC object or secret scope is outside the ceiling it raises `ScopeDenied`.
  **No `_apx_scope` → guard is a no-op** (back-compat, FR-6).
- **FR-5 — Audit event (logged + traced).** On denial the guard stamps the active
  span (`current_active_span()`) via `set_audit_attrs` with new `AuditAttrs`
  `SCOPE_ACTION` (`"deny"`), `SCOPE_REASON`, `SCOPE_OBJECT` (the out-of-scope
  identifier), and logs one WARNING — mirroring the `WATCHDOG_ACTION`/`_REASON`
  pattern. New keys added to `AuditAttrs` + `_KWARG_TO_KEY` (unknown-kwarg
  fail-loud stays intact).
- **FR-6 — Back-compat.** A tool declaring no `identity`/`scope`/`secret_scopes`
  attaches no `_apx_scope`; `ScopeGuard` skips it and `validate_tool_scope` is a
  no-op. Identity for undeclared tools stays whatever `infer_operation_authorization`
  chooses today. Purely additive.

### Non-functional
- **NFR-1 — Transparent by default.** No scope declared = zero new behavior and no
  new config required. A declared scope needs no credential/grant hand-wiring —
  identity maps through the existing `execution`/OBO path.
- **NFR-2 — Clear errors.** Compile-time over-scope names the tool, the offending
  identifier, and the ceiling that rejected it. `scope_denied:` messages name the
  tool + the out-of-scope object, actionable to the agent and in the audit log.
- **NFR-3 — No new dependencies.** Reuse `build_tool`/`ResourceSpec`/`ToolMetadata`,
  the `before_tool` guard surface, `_audit.py`, and the middleware containment.
- **NFR-4 — No lint-ban regressions.** No `.get(k, "")`, no `x or ""`, no invented
  env defaults, no `object` annotations, no `tuple[...]` returns, no skipped tests.

## Design
### Architecture
```
[[tool.apx.tools]] table                       Python: build_tool(execution=, scope=)
        │ _tool_config._build_one pops identity/scope/secret_scopes
        ▼
  _tool_scope.parse_tool_scope → ToolScope (frozen)   ── attach_scope → fn._apx_scope
        │                                              └─ identity → build_tool(execution=…)
        ▼ load_config_tools + deploy preflight
  validate_tool_scope(fn)  ── over-scoped/invalid → ToolConfigError (blocks build/deploy)
        │
        ▼ runtime (before_tool)
  ScopeGuard.for_tool()  ── out-of-scope → raise ScopeDenied(PermissionError)
        │                     └─ set_audit_attrs(span, scope_action="deny", …) + log WARNING
        ▼
  _governance_exception_middleware._CONTAINED contains it → "scope_denied: …" ToolMessage
```
- **New file:** `python/src/apx_agent/_tool_scope.py` — `ToolScope` (frozen
  dataclass: `identity`, `catalogs`/`schemas`/`tables`/`functions`,
  `secret_scopes`), `parse_tool_scope`, `attach_scope`/`get_scope` (mirror
  `_resources.attach_resources`/`get_resources`), `validate_tool_scope`,
  `ScopeGuard`, `ScopeDenied(PermissionError)`, `in_scope(scope, resource_ref) ->
  bool`.
- **Edited:** `_tool_config._build_one` (parse + attach + identity→execution),
  `load_config_tools` (call `validate_tool_scope`), `_tool_factory.build_tool`
  (accept `scope=`/`secret_scopes=`), `_audit.AuditAttrs`/`_KWARG_TO_KEY`
  (SCOPE_* keys), the deploy preflight (`_compile_run.py` / `agents deploy`),
  and `_guards.py`/compile wiring so `ScopeGuard.for_tool()` joins the
  `before_tool` chain when any tool declares a scope.
- Reuse `_governance_exception_middleware` verbatim (`PermissionError` already in
  `_CONTAINED`) — no middleware change.

### Interface changes
- `[[tool.apx.tools]]` optional keys: `identity`, `scope` (`{catalogs, schemas,
  tables, functions}`), `secret_scopes`.
- `build_tool(..., scope: ToolScope | None = None, secret_scopes: list[str] | None
  = None)` — additive; `execution=` already exists for identity.
- New public error `ScopeDenied` exported from `apx_agent`.
- New `AuditAttrs.SCOPE_ACTION` / `SCOPE_REASON` / `SCOPE_OBJECT`
  (`apx.scope.action` / `apx.scope.reason` / `apx.scope.object`).

### `scope_denied` contract (distinct from tools-route `ToolError`)
Runtime: raise `ScopeDenied("scope_denied: tool 'q' may not touch "
"'main.finance.ledger' (scope: catalog 'sales')")`. Contained into a
`ToolMessage(status="error", content="Error: scope_denied: …")`. Distinct from
`ToolError` **by type** — a `scope_denied` is an authorization ceiling breach, not
an operational finding — so consumers can branch on it. Audit span carries
`apx.scope.action="deny"`, `apx.scope.object=<identifier>`, `apx.scope.reason=<why>`.

## Acceptance Criteria
- [ ] **AC-1 (cheap).** *Given* a `[[tool.apx.tools]]` table with `identity="obo"`,
  a `scope` table, and `secret_scopes`, *When* `load_config_tools` builds it,
  *Then* `get_scope(fn)` returns a `ToolScope` carrying those values and
  `fn._apx_tool.execution == "user"`. Verifiable: true. test_type: pytest.
  gate_file: `python/tests/test_tool_scoped_auth.py`. gate_test:
  `test_declaration_parses_into_scope`.
- [ ] **AC-2 (cheap).** *Given* a tool whose `_apx_resources` declares
  `uc_table` `main.finance.ledger` but whose `scope` allows only catalog `sales`,
  *When* `validate_tool_scope`/`load_config_tools` runs, *Then* it raises
  `ToolConfigError` naming the tool + the offending identifier (over-scope
  hard-fail). Verifiable: true. test_type: pytest. gate_file:
  `python/tests/test_tool_scoped_auth.py`. gate_test:
  `test_compile_fails_on_overscope`.
- [ ] **AC-3 (cheap).** *Given* a scoped tool and a call targeting an out-of-scope
  UC object, *When* `ScopeGuard.for_tool()` runs in `before_tool`, *Then* it
  raises `ScopeDenied` and the `_governance_exception_middleware` contains it as a
  `ToolMessage(status="error")` whose content starts `Error: scope_denied:` (turn
  stays alive, no 500). Verifiable: true. test_type: pytest. gate_file:
  `python/tests/test_tool_scoped_auth.py`. gate_test:
  `test_out_of_scope_raises_scope_denied`.
- [ ] **AC-4 (cheap).** *Given* `ScopeDenied` and `ToolError`, *When* their types
  are compared, *Then* `ScopeDenied` is NOT a `ToolError` (distinct contract),
  IS a `PermissionError` (rides `_CONTAINED`), and its message is
  `scope_denied:`-prefixed. Verifiable: true. test_type: pytest. gate_file:
  `python/tests/test_tool_scoped_auth.py`. gate_test:
  `test_scope_denied_contract_distinct`.
- [ ] **AC-5 (cheap).** *Given* `identity` values `sp` / `obo` / `named-sp:reader`,
  *When* the tool is built, *Then* `execution` maps to `"service"` / `"user"` /
  `"service"`(+named), and a declared identity conflicting with the inferred one
  raises (reusing `_apps_authorization`'s conflict). Verifiable: true. test_type:
  pytest. gate_file: `python/tests/test_tool_scoped_auth.py`. gate_test:
  `test_identity_mode_maps_to_execution`.
- [ ] **AC-6 (cheap, back-compat).** *Given* a tool declaring no
  identity/scope/secret_scopes, *When* built and run, *Then* `get_scope(fn)` is
  `None`, `ScopeGuard` is a no-op, and behavior is byte-for-byte today's
  unrestricted path. Verifiable: true. test_type: pytest. gate_file:
  `python/tests/test_tool_scoped_auth.py`. gate_test:
  `test_undeclared_scope_unrestricted`.
- [ ] **AC-7 (cheap, reality/Ctk read-after-write).** *Given* a scope denial
  fires, *When* the trace/span and returned tool result are read back, *Then*
  the audit event is REAL — the span carries `apx.scope.action="deny"` +
  `apx.scope.object` + `apx.scope.reason` AND the returned `ToolMessage` is a
  non-empty `scope_denied:` error (asserted via `ctk.verify`/`Artifact`, not a
  bare exit-0 or `.exists()`). Verifiable: true. test_type: pytest. gate_file:
  `python/tests/test_tool_scoped_auth_reality_ctk.py`. gate_test:
  `test_scope_denial_audit_and_error_are_real`.
- [ ] **AC-8 (live, gated).** *Given* a tool scoped to catalog `sales` running
  OBO against a live workspace, *When* it queries `main.finance.ledger`, *Then*
  the call is refused with `scope_denied` and a real UC-denied audit row is
  observable. Verifiable: maybe (manual/live). test_type: pytest. gate_file:
  `python/tests/test_tool_scoped_auth_reality_ctk.py`. gate_test:
  `test_live_uc_scope_denial`. **skip_reason:** requires `APX_CAPS_PROFILE`
  (live UC grants) — cheap tier (AC-1..7) proves the guard + contract + compile
  fail; this proves real UC denial. Live capability check:
  `python/checks/prove_tool_scoped_auth.py` (tier: live in `capabilities.yaml`).

## Risks
- **Scope-match semantics ambiguity** (catalog-only vs fully-qualified
  `catalog.schema.table` matching) → `in_scope` must be explicit: a `tables`
  entry matches its exact FQN; a `catalogs`/`schemas` entry matches any object
  under it. *Mitigation:* encode the precedence in `in_scope` + AC-2/AC-3 fixtures.
- **`ScopeGuard` can only see the UC object the tool actually targets** — for a
  free-form SQL tool the target isn't known until the SQL is parsed. *Mitigation:*
  v1 enforces on `_apx_resources`-declared objects + secret scopes; SQL-body
  parsing is a follow-up (note the ceiling in a `ponytail:` comment).
- **Identity conflict false-positives** if a factory infers `"service"` but the
  author declares `obo`. *Mitigation:* reuse the existing conflict message; AC-5
  covers it.
- **Live UC denial unprovable in PR CI** (no `APX_CAPS_PROFILE`). *Mitigation:*
  AC-8 is live-gated with `skip_reason`; cheap ACs are the merge gate.

## Open Questions
- [ ] Confirm the `named-sp:<name>` runtime resolution path — does an App
  container expose a second SP's credentials, or is a named narrow SP v1-declared
  only (compile-validated) and resolved later? Default: **declared + compile-
  validated in v1**, runtime resolution behind the same `execution="service"` path
  (escalate if a second live credential source is required).

---

## Agent Handoff
```json
{
  "prd_version": "1.0",
  "goal": "Each apx-agent tool can declare its own execution identity (sp|obo|named-narrow-SP) and a UC-object + secret-scope ceiling, enforced at compile time (deploy hard-fails on invalid/over-scoped) and runtime (out-of-scope call refused with a distinct scope_denied error that is logged + traced); undeclared scope stays today's unrestricted behavior. Proven by pytest.",
  "success_criteria": ["per-tool identity+scope+secret-scope declaration", "compile-time over-scope/invalid hard-fail", "runtime scope_denied refusal distinct from ToolError, contained not 500", "audit event logged+traced on denial", "back-compat: undeclared = unrestricted", "framework-transparent: good defaults, clear errors, no hand-wiring"],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest -k scoped_auth green AND make check green",
    "progress_metric": "failing test count",
    "known_ceiling": "live UC-denial AC (AC-8) unprovable without APX_CAPS_PROFILE; ScopeGuard v1 enforces on _apx_resources-declared UC objects + secret scopes, not on parsed free-form SQL bodies",
    "re_represented": false
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "[[tool.apx.tools]] identity/scope/secret_scopes parse into ToolScope on fn._apx_scope; obo -> execution 'user'", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth.py", "gate_test": "test_declaration_parses_into_scope" },
    { "id": "AC-2", "description": "over-scoped tool (ResourceSpec outside declared scope) hard-fails build with ToolConfigError naming tool+identifier", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth.py", "gate_test": "test_compile_fails_on_overscope" },
    { "id": "AC-3", "description": "out-of-scope call raises ScopeDenied, contained by _governance_exception_middleware as scope_denied ToolMessage (no 500)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth.py", "gate_test": "test_out_of_scope_raises_scope_denied" },
    { "id": "AC-4", "description": "ScopeDenied is distinct from ToolError (not a subclass), is a PermissionError, message scope_denied:-prefixed", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth.py", "gate_test": "test_scope_denied_contract_distinct" },
    { "id": "AC-5", "description": "identity sp/obo/named-sp maps to ExecutionIdentity service/user/service; conflict with inferred raises", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth.py", "gate_test": "test_identity_mode_maps_to_execution" },
    { "id": "AC-6", "description": "undeclared scope -> get_scope None, ScopeGuard no-op, unrestricted (back-compat, additive)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth.py", "gate_test": "test_undeclared_scope_unrestricted" },
    { "id": "AC-7", "description": "Ctk read-after-write: denial emits a REAL audit event (span apx.scope.action=deny + object + reason) and a real scope_denied ToolMessage, asserted via ctk.verify/Artifact not exit-0", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth_reality_ctk.py", "gate_test": "test_scope_denial_audit_and_error_are_real" },
    { "id": "AC-8", "description": "live: OBO tool scoped to catalog sales is refused with scope_denied when querying main.finance.ledger; real UC-denied audit observable", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_tool_scoped_auth_reality_ctk.py", "gate_test": "test_live_uc_scope_denial", "skip_reason": "requires APX_CAPS_PROFILE (live UC grants); cheap ACs prove guard+contract+compile-fail, this proves real UC denial. Live check: python/checks/prove_tool_scoped_auth.py" }
  ],
  "must_have": ["new python/src/apx_agent/_tool_scope.py (ToolScope, parse_tool_scope, attach_scope/get_scope, validate_tool_scope, ScopeGuard, ScopeDenied(PermissionError), in_scope)", "parse identity/scope/secret_scopes in _tool_config._build_one; validate in load_config_tools + deploy preflight", "reuse build_tool(execution=) for identity mapping (obo->user, sp/named-sp->service) and _apps_authorization conflict-raise", "ScopeDenied distinct from ToolError but PermissionError so _governance_exception_middleware._CONTAINED contains it", "AuditAttrs.SCOPE_ACTION/SCOPE_REASON/SCOPE_OBJECT + _KWARG_TO_KEY; stamp via set_audit_attrs on current_active_span + one WARNING log", "back-compat: no _apx_scope => no enforcement", "capabilities.yaml entry (cheap reality check + live prove_tool_scoped_auth.py)"],
  "out_of_scope": ["external-host/outbound-network allowlist", "deny-by-default (undeclared stays unrestricted)", "per-hop A2A re-scoping", "auto-provisioning UC grants", "parsing free-form SQL bodies to extract targeted UC objects (v1 enforces on declared ResourceSpec + secret scopes)"],
  "constraints": {
    "tech_stack": "Python, Databricks SDK, LangChain/LangGraph, MLflow tracing, pytest, ctk",
    "key_files": ["python/src/apx_agent/_tool_scope.py", "python/src/apx_agent/_tool_config.py", "python/src/apx_agent/_tool_factory.py", "python/src/apx_agent/_resources.py", "python/src/apx_agent/_apps_authorization.py", "python/src/apx_agent/_obo.py", "python/src/apx_agent/_guards.py", "python/src/apx_agent/_compile.py", "python/src/apx_agent/_compile_run.py", "python/src/apx_agent/_audit.py", "python/src/apx_agent/_errors.py", "python/src/apx_agent/__init__.py", "python/tests/test_tool_scoped_auth.py", "python/tests/test_tool_scoped_auth_reality_ctk.py", "python/checks/prove_tool_scoped_auth.py", "python/capabilities.yaml"],
    "patterns": "mirror _resources.attach_resources/get_resources for attach_scope/get_scope (fn._apx_scope); reuse build_tool(execution: ExecutionIdentity) already at _tool_factory.py:49 for identity; parse extra table keys in _tool_config._build_one like it pops 'type'; raise ToolConfigError (ValueError) for config/over-scope failures matching the module's idiom; ScopeDenied subclasses PermissionError (like ApxIdentityError/ApprovalRequired) so _compile._governance_exception_middleware._CONTAINED contains it into an error ToolMessage; add before_tool guard ScopeGuard.for_tool() alongside WatchdogGuard.for_tool() in _guards.py; audit via _audit.AuditAttrs (add SCOPE_* + _KWARG_TO_KEY) + set_audit_attrs(current_active_span(), ...) mirroring the WATCHDOG_ACTION/_REASON watchdog pattern; do NOT reuse ToolError as the scope error; capabilities.yaml cheap+live entries with real read-back checks",
    "lint_bans": "no .get(k,\"\"), no `x or \"\"`, no invented env defaults, no `object` annotations, no `tuple[...]` returns, no skipped tests"
  },
  "preferred_skills": ["ctk", "databricks-unity-catalog", "databricks-apps-python"],
  "escalate_on": [
    "named-sp:<name> requires a second live credential source in the App container not reachable through the existing execution='service' path",
    "in_scope match semantics (catalog vs schema vs fully-qualified table/function) are ambiguous for a required case",
    "ScopeGuard cannot see the tool's targeted UC object without parsing a free-form SQL body (a case that must be enforced in v1)",
    "declared identity vs _apps_authorization inferred identity conflict has no single clear resolution",
    "architectural decision not covered by this PRD"
  ],
  "loop_guards": {
    "max_iterations": 8,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```
