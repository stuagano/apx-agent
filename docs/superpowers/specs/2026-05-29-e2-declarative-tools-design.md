# Design: E2 · Declarative Tools (`[tool.apx.tools]`)

**Date:** 2026-05-29
**Status:** Spec (design) — ready for implementation plan after the chokepoint decision (below) is confirmed
**Scope:** `apx-agent` engine
**Backing analysis:** `docs/engine-scope/02-declarative-tools.md` (full schema, factory inventory, dispatcher, governance, error handling, test plan — treat as the detailed design reference; this spec is the decision/reconciliation layer on top of it)

---

## Context & relationship to E1

E1 (Template protocol, PR #111) shipped the `Template` registry and, critically, the **shared config→instance seam `apply_config_knobs(agent, config)`** in `_wiring.py`. E2 is the second rung of the agent-as-config ladder: let an agent's *resource-reference-tier* tools (Genie, Vector Search, UC functions, SQL, HTTP/OpenAPI, MCP, foundation-model, jobs) be declared as **data** in `[[tool.apx.tools]]` instead of hand-written `agent.py`. Every such factory already takes plain data args and returns a stamped callable (see scope doc §1) — E2 moves that configuration into TOML so a config generator (Coworker) emits it without generating Python.

## The decision E2 forces (reconciling with E1's seam)

The scope doc (§3.3) concluded the merge must run at a chokepoint hit by **all three runtimes** — serve (`setup_agent`), log/deploy (`log_agent`), and model-serving predict (captures the agent at log time) — and recommended a `finalize_agent` helper called from inside `log_agent`.

**E1 did not build `finalize_agent`. It built `apply_config_knobs`, called from `setup_agent` and from the `apx deploy` *CLI command* (before `log_agent`) — NOT from inside `log_agent` itself.** Therefore E1's knob/instruction overlay has a latent gap: a **direct `log_agent(agent)` call from a notebook or Coworker** (a public API, `__init__.py`) bypasses the CLI wrapper and never runs `apply_config_knobs`.

**Decision for E2 (recommended): promote `apply_config_knobs` into a unified `finalize_agent(agent, config, pyproject_path=None)`** that runs, idempotently:
1. `apply_config_knobs` (E1's knobs + persona overlay — moved in, not duplicated),
2. `merge_config_tools` (E2),
3. (later) memory/guards attach (E3).

Call `finalize_agent` from **inside `log_agent`/`mlflow_resources_for`** (fixes the direct-call gap for E1 *and* E2 at once), from **`setup_agent`** (serve), and from **`apx info`**. The `apx deploy` CLI then no longer needs its own `apply_config_knobs` call — `log_agent` covers it.

This is the single most important E2 design point: **E2 is where the seam graduates from "knobs in the CLI" to "full finalize inside `log_agent`."** It retires E1's CLI-only placement and closes the notebook/Coworker gap.

## Design summary (full detail in scope doc §3–§5)

- **Schema:** `[[tool.apx.tools]]` array-of-tables, sibling of `[tool.apx.agent]` (NOT routed through `AgentConfig`, whose loader drops unknown keys). Discriminator `type`; remaining keys are the factory's kwargs. `$ENV_VAR` resolved on string leaves via the existing `_resolve_env_var`.
- **Dispatcher** (`_tool_config.py`, new): a `type → factory` registry; call each factory with all config keys **by keyword** (every factory accepts its first positional as positional-or-keyword, so no multi-positional special-casing). Toolkit factories (`uc_function_toolkit`, `mcp_toolkit`, `openapi`, `jobs`) return lists → flatten. No per-type Pydantic models — the factories are the validation surface; a bad/missing kwarg surfaces as a wrapped `ToolConfigError`.
- **Merge:** append survivors to `agent._tool_fns`, dedup by `__name__` (code-wired tools win, config additive), and **rebuild `_analyzed`** via a new `LlmAgent._register_tool(fn)` so the A2A card / MCP surface / per-tool routes (which read `_analyzed`, not `_tool_fns`) see config tools. Composition roots (Sequential/Router) → warn + skip (mirrors the persona-overlay skip E1 just added).
- **Governance:** config tools are the *same* factory callables → per-user OBO + UC grants apply identically; their `ResourceSpec`s flow into the logged `resources=[...]` **only because** the merge runs at the `log_agent` chokepoint (the whole point of the decision above). MCP/openapi declare no resources and do network I/O at factory time.
- **Errors:** unknown `type` / missing kwarg → loud `ToolConfigError`; two same-named config tools → require explicit `name=`; the three I/O factories (`mcp_*`, `openapi`) → per-table skip-with-warning, with an `APX_TOOLS_STRICT=1` opt-in to make them hard failures for deploy validation.

## Resolved decisions (carried from scope §7 open questions)

- **Q1 section name** → `[[tool.apx.tools]]` (sibling). Confirm with Coworker's emitter.
- **Q5 I/O types at log time** → include `mcp_*`/`openapi` in finalize with skip-with-warning (so the model-serving runtime also gets the callables), strict-mode to gate deploys that require them.

## Open questions (need a decision before/within the plan)

- **The chokepoint promotion itself** — confirm `finalize_agent`-inside-`log_agent` (recommended) vs keeping `apply_config_knobs` CLI-only and adding a separate tool merge. (Strongly recommend the unified helper; it also fixes E1's notebook gap.)
- **Q2** constructor sugar (`load_config_tools=True`) — defer; external-only is lower surface.
- **Q3** composition-root per-leaf targeting (`agent = "<leaf>"`) — defer to v2; first Coworker agents are flat `LlmAgent`s.
- **Q4** trust model — is Coworker-generated `pyproject.toml` trusted? Decides whether the `openapi`/`mcp_*` host allow-list (`APX_TOOLS_ALLOWED_HOSTS`) is opt-in (trusted default) or opt-out.
- **Q6** also route config `sub_agents` resources through the shared helper (fixes the analogous precedent gap, scope §2).

## Effort

**M (~350–550 LOC incl. tests)** per scope §7. The genuine work is the chokepoint promotion + `_analyzed` refresh + the log-path/predict-path governance regression tests — not new tool machinery.

## Dependencies

Builds on E1 (`apply_config_knobs`, the registry). Should land **before or with** E3, since E3's memory/guards attach reuse the same `finalize_agent` chokepoint this spec introduces.
