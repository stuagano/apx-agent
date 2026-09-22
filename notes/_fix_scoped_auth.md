# Fix pass: tool-scoped-auth — close the runtime-enforcement gap + 3 correctness bugs

The source + compile-time validation for tool-scoped auth is complete and its
cheap-tier tests pass. Two independent reviews found the runtime half is not
actually connected, plus three correctness bugs. Fix all of the following, add a
**served-path** test (not an isolated-guard test), and re-prove.

Context files: `prd_tool_scoped_auth.md` (spec), `_tool_scope.py` (the feature).

## MUST FIX

### 1. CRITICAL — wire ScopeGuard into the served before_tool chain
`ScopeGuard` (`python/src/apx_agent/_tool_scope.py:307`) is never constructed
outside tests/checks/docstring. The served guard chain is composed in
`python/src/apx_agent/_guards.py` `build_config_guards()` (~L406-453) and merged
in `python/src/apx_agent/_wiring.py` (~L213-230, `apply_config_guardrails`),
alongside `WatchdogGuard.for_tool()`.

- Auto-compose `ScopeGuard(tools).for_tool()` into `before_tool` where the
  agent's tool fns are known (same place Watchdog is composed).
- Gate on the existing `ScopeGuard(...).active` property (`_tool_scope.py:338`)
  so agents with NO scoped tools are byte-for-byte unchanged (back-compat / AC-6).
- The guard only gets `(name, args)`, so it must be built from the tool fns to
  map name -> scope + resources (see the `Discoveries` note already in
  `_relentless_state.md`).
- Do NOT require the developer to hand-wire it — "declared, not wired."

### 2. IMPORTANT — remove `"scope"` from `_SECRET_ARG_KEYS`
`python/src/apx_agent/_tool_scope.py:268`: `_SECRET_ARG_KEYS = {"secret_scope", "scope"}`.
`scope` is a common non-secret arg name (OAuth/search/config scope) and causes a
spurious `ScopeDenied` on legitimate calls. Keep only `secret_scope`.

### 3. IMPORTANT — case-fold UC identifier matching (over-denial bug)
`in_scope` (`_tool_scope.py:239-244`) compares raw segments; UC identifiers are
case-insensitive, so `main.finance.LEDGER` is wrongly denied against ceiling
`main.finance.ledger`. Normalize BOTH sides with `.casefold()` (and strip
backtick quoting if present) before comparing catalogs/schemas/tables/functions.
LEAVE `secret_in_scope` (`:252-254`) case-sensitive — Databricks secret-scope
names are case-sensitive.

### 4. IMPORTANT — validate scope on the build_tool() Python API path
`build_tool(scope=...)` (`python/src/apx_agent/_tool_factory.py:86-92`) attaches
the scope but never calls `validate_tool_scope`, so an over-scoped tool built via
the Python API isn't caught at build time (AC-2 only holds for the config path).
Call `validate_tool_scope(call)` at the end of `build_tool` when a scope was
attached.

## SHOULD

### 5. Comment the attach_scope execution override
`attach_scope` (`_tool_scope.py:206-212`) silently `replace()`s
`ToolMetadata.execution` when a scope declares `identity`, with no conflict
detection against an explicitly-set `execution`. Add a `ponytail:`-style comment
naming this so it reads as intent, not oversight. (No behavior change unless you
see a clean way to detect the conflict — do NOT over-engineer.)

## GATE (read-after-write, Ctk)
- Add a NEW test that drives a **compiled/served agent** with a scoped tool and
  asserts an out-of-scope call is refused as a `scope_denied` ToolMessage through
  the real serve path — NOT by constructing `ScopeGuard` by hand. This is the
  test that would have caught finding 1. Put it in
  `python/tests/test_tool_scoped_auth.py` or the reality-ctk file as fits the
  existing pattern.
- Keep the existing isolated-guard tests.
- Adjust the ctk reality test if fix #4 changes when the over-scoped build raises.
- `cd python && uv run pytest -k scoped_auth` green.
- `make check` green.
- Re-prove caps cheap tier:
  `cd python && uv run --frozen python -c "import sys; sys.path.insert(0,'..'); from caps.cli import main; raise SystemExit(main(['verify','--tier','cheap']))"`
  — `tool-scope-denies-out-of-scope` must pass fresh.

## DO NOT
- Touch unrelated live-tier caps (uc-publish, ka/genie-obo-live, canary,
  watchdog, trace-export) — pre-existing, need APX_CAPS_PROFILE, not this change.
- Weaken or skip any test to get green.
- Deploy or run anything against a live workspace.
