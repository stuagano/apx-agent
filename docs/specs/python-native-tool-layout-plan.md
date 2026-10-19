# Python-native Tool Layout Implementation Plan

**Goal:** Ship Python-authored tools with explicit imports, staged package support,
no automatic legacy TOML tool loading, and unchanged governance guarantees.

**Spec:** `docs/specs/python-native-tool-layout.md`

**Architecture:** Existing factories and agent constructors remain the interface.
The existing YAML/spec generator emits Python source, not TOML tool rows or a
new runtime registry. AgentKit's manifest remains generated runtime output.

## Work ownership and verification

1. **Generation** — `_project_gen.py`, a focused source-rendering helper if
   needed, and project/graph generation tests. Generate explicit factory calls,
   preserve env references at runtime and scoped metadata, flatten toolkits,
   import tool packages in generated agents, and reject authored-file collisions.
   Observe focused failing tests before changing behavior, then execute generated
   modules to prove names, behavior, and resource metadata.
2. **Governance** — `_agent_identity.py`, existing host-check helper,
   `http_tools.py`, `mcp_consume.py`, dedicated governance regressions. Move the
   existing host policy to direct factory entry points before network I/O and
   preserve all scope fields during identity inheritance. Prove unauthorized
   calls are denied, not merely that metadata contains a caller list.
3. **Staging** — `_agentbricks_deploy.py`, `test_tool_decls_layout.py`. Include
   `tools/`; reject module/package ambiguity; retain symlink and ownership checks.
   Import a relocated staged fixture in a new process after removing its source.
4. **Runtime and consumers** — remove `merge_config_tools`, reject legacy tool
   TOML before factory I/O, update doctor resource inspection and CLI/chat/wiring
   tests to use Python tools. Keep programmatic spec-conversion helpers required
   by existing YAML inputs, not a parallel filesystem authoring format. Migrate
   scaffolding comments and current documentation; do not rewrite historic PRDs.
5. **Integration** — inspect all worker diffs, run focused suites, `make check`,
   `caps status`/`caps verify`, and independent defect review. Fix confirmed
   regressions and rerun relevant gates. No deployments, dependency updates,
   unrelated branch cleanup, or changes in the CTK checkout.

All existing paths must be checked for symlinks before mutation. Preserve unrelated
untracked artifacts. Do not skip or weaken tests. Progress is reported in the
session; this plan records scope, not a claim that verification has completed.
