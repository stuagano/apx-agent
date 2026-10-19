# Python-native tool declaration layout

Date: 2026-10-19
Status: Approved for implementation. Clean Python-native tool authoring; legacy TOML tool declarations fail with migration guidance.

## Decision and rationale

The user approved Python-authored tools in a discoverable `tools/` package,
explicit Python imports, and continued generation of the AgentKit runtime
`agent.toml`. Do not introduce the previously contemplated per-tool TOML files,
JSON schema, directory-scanning loader, or mandatory folders per tool kind.

"Declared, not wired" describes who owns runtime wiring; it does not require a
non-Python declaration language. Reuse the existing typed tool factories and
agent constructors rather than inventing a second Python declaration model.

The runtime does not force this move out of `pyproject.toml`: the AgentKit
manifest is `.build/agent.toml`. Separation of authored tools from packaging and
generated runtime contracts is the reason for the layout.

## Authored and generated files

```text
agent.py                 # Explicitly imports tools and composes the agent
tools/                   # Ordinary Python package
  __init__.py
  sales.py               # Existing factory call or custom Python tool
  products.py
pyproject.toml           # Packaging; existing non-tool APX settings remain
.build/agent.toml        # Generated runtime contract; never hand-maintained
```

Tool module names are organizational, not tool identities. Preserve explicit
factory names and callable metadata. Group related tools when useful; do not
require a file or empty directory for every kind. Python toolkit factories may
return lists: composition must preserve their existing flattening semantics.

## Scope

1. Make Python tool modules and explicit agent composition the canonical layout
   for the affected project generator, examples, and authoring documentation.
2. Native staging must copy `tools/` and preserve importability from the staged
   application independently of the source checkout.
3. Reuse existing validation, runtime compilation, authorization planning, and
   generated-manifest paths. Close any demonstrated governance gap in the shared
   Python path before migrating affected declarations.
4. Revise the unfinished `python/tests/test_tool_decls_layout.py` to prove the
   approved Python contract, not the abandoned TOML directory format. Preserve
   validation and governance coverage rather than merely removing failing tests.

## Migration policy

New generated projects and migrated examples no longer emit tool definitions in
`[[tool.apx.tools]]`. Remove automatic loading/merging of those declarations.
Reject their presence before factory I/O with actionable instructions to declare
Python tools in `tools/` and import them in `agent.py`. This is an intentional
breaking authoring change, not a dual-format transition.

Keep YAML/spec input compilation where already supported, but generate explicit
Python factory calls rather than TOML rows or runtime dictionary dispatch.
Existing internal programmatic conversion utilities are not a second filesystem
authoring path. Mixed Python/TOML projects must fail rather than silently choosing
one definition.

Do not move `[tool.apx.agent]`, deployment settings, or every other APX setting
out of `pyproject.toml` in this tool-layout change. Python-native tool authoring
is not a claim that all existing project configuration has already migrated.

## Behavioral requirements

- Use existing public Python factory/decorator interfaces. Do not route new
  authoring through string type tags and dictionary dispatch merely to recreate
  the old TOML rows in Python.
- Preserve per-tool identity, scope ceilings, secret scopes, caller restrictions,
  resource metadata, approval behavior, and audit attribution.
- Preserve relevant network host restrictions before factory-time I/O. Direct
  imports must not accidentally bypass checks previously applied by the loader.
- Python import can execute code. This is trusted project code, not a promise of
  static/no-execution inspection. Do not introduce additional eager discovery or
  remote calls merely to discover files in `tools/`.
- No secrets in generated source or manifests. Missing required configuration
  must fail clearly rather than invent values or omit tools silently.
- Do not overwrite an existing user-authored `tools.py` or `tools/` package when
  generating into an existing project. Avoid module/package name collisions;
  report the conflict rather than guessing which should win.
- Preserve existing `tools.py` deployment support for projects that use it.

## Verification and acceptance criteria

1. A representative generated project contains an importable `tools/` package,
   explicit imports in `agent.py`, and no new `[[tool.apx.tools]]` definitions.
   Include existing skill-tool generation in coverage if migrated by that path.
2. Importing/composing the generated Python agent yields the intended tool names,
   callable behavior, descriptions, and resource/governance metadata. Use local
   fakes at external service seams; no live Databricks calls are required.
3. Toolkit composition and a custom Python tool work without a new registry.
4. A staged project imports in a fresh process with the source checkout absent
   from its import path. Assert `tools/` contents and imported module locations,
   not only that staging exited successfully.
5. Equivalent Python-authored and legacy tool configurations produce equivalent
   authorization requirements and generated native manifest fields, including a
   denied/unauthorized case. Existing approval and audit regression tests pass.
6. Legacy tool-TOML projects fail before factory I/O with migration guidance;
   Python tools remain stable across repeated finalization.
7. Tests exercise missing configuration, naming collisions, and generation into
   an existing project without destroying authored work.
8. Run focused tests followed by `make check`, and applicable capability checks,
   before claiming implementation complete. Read back generated artifacts.

## Evidence and implementation touchpoints

- `python/src/apx_agent/_tool_config.py`: existing TOML reader, factory mapping,
  scope attachment, host checks, and Python-first merge precedence.
- `python/src/apx_agent/_project_gen.py`: `_build_pyproject` currently emits tool
  and skill rows; `generate_project` writes Python agent source.
- `python/src/apx_agent/_agentbricks_deploy.py:84`: `stage_native_source` includes
  `tools.py` but not `tools/` in its standard source list.
- `python/src/apx_agent/_durable_agent.py:35`: `build_native_manifest` generates
  the AgentKit contract from existing configuration and authorization plans.
- `python/tests/test_project_gen.py`: existing generator assertions for TOML
  tools and skill entries must be deliberately updated, not skipped.

## Governance review findings

Read-only inspection found migration-sensitive behavior; no tests have yet been
executed to reproduce these findings.

- `build_tool(scope=ToolScope(...))` already attaches and validates scope on the
  Python path (`_tool_factory.py:46-96`). Reuse it and preserve factory-attached
  resources; do not mistake bare `@tool` or `attach_scope` for equivalent scope
  validation. Apply scope to each member of toolkits.
- The host policy currently lives in the TOML loader, before network factory
  construction (`_tool_config.py:49-63,172-174`). Direct network factories need
  equivalent pre-I/O enforcement for migration parity. This is the existing
  limited host policy, not a universal network sandbox.
- Agent identity inheritance reconstructs `ToolScope` without copying `callers`
  (`_agent_identity.py:64-71`). A regression test must prove caller restrictions
  survive inheritance. This is a pre-existing shared-path issue: flag it for
  explicit inclusion or a prerequisite fix rather than hiding it in layout work.
- Caller restrictions and scope guards have different enforcement surfaces.
  Preserve and test their actual behavior; do not claim universal interception
  merely because Python metadata exists.

## Non-goals

No deployment, new runtime, new tool registry, automatic Python file discovery,
new TOML tool schema, wholesale agent-configuration rewrite, unrelated branch
cleanup, or changes to the separate CTK worktree.
