# Design: E3 · Declarative Agent Config (template-as-config + memory + guards)

**Date:** 2026-05-29
**Status:** Spec (design) — larger than E1/E2; may split into sub-plans (see "Decomposition")
**Scope:** `apx-agent` engine
**Backing analysis:** `docs/engine-scope/03-declarative-memory.md` and `docs/engine-scope/04-declarative-guards.md` (full schemas, store factories, the principal-isolation gap, guard mechanics — the detailed design reference; this spec is the decision/reconciliation layer)

---

## Context & relationship to E1/E2

This is the top engine rung: with E1 (Template registry + `apply_config_knobs` seam) and E2 (`[[tool.apx.tools]]` + the unified `finalize_agent` chokepoint), E3 makes an agent **fully definable from `[tool.apx.agent]`** — the substrate a CoworkerSpec compiles to. E3 has three config surfaces that all attach at the **same `finalize_agent` chokepoint E2 introduces**:

1. **Template-as-config** — `[tool.apx.agent].template = { name = "...", ... }` → `template_registry.build(name, spec, ws)` (E1's registry) → a wired leaf agent.
2. **Memory / example / session backends** — `[tool.apx.agent].memory|example|session` → auto-built stores + auto-attached tools (scope doc 03).
3. **Built-in guards** — `[tool.apx.agent.guardrails]` → allow/deny lists, rate limit, injection detection (scope doc 04).

## Decomposition (this spec is bigger than one plan)

Recommend three sub-plans, sequenced, all hanging off the E2 `finalize_agent` chokepoint:

- **E3a · Template-as-config** (smallest; depends only on E1's registry). `AgentConfig` gains a `template: TemplateRef | None` field (`{name, spec-dict}`). `finalize_agent` (or `setup_agent`/`log_agent`) builds the agent from the registry when the user's `module` points at a template ref rather than a constructed agent. Resolves the "an agent IS a template instance" path. Persona envelope (E1) layers on top via the existing `apply_config_knobs` overlay — the role/persona split already designed in E1.
- **E3b · Declarative memory/session** (scope 03; **M, ~450–650 LOC**; the riskiest piece in the whole program).
- **E3c · Declarative guards** (scope 04; **S–M, ~260 LOC**; lowest risk).

E3c or E3a is the right first slice (small, self-contained). E3b should be prototyped before committing (see risk below).

## E3b — the load-bearing risk (memory): per-request principal threading

The central new primitive (scope 03 §4): a config-built memory tool **cannot see the per-request OBO principal** today. `make_memory_tools` needs a `principal_id_resolver() -> str|None`, but inside a compiled LangGraph there is no FastAPI `Request`, and tool closures don't receive the `CompileContext` that carries identity. A naive `lambda: None` collapses all users into `NO_PRINCIPAL` (fails safe — no leak — but memory is useless).

**Recommended fix (scope 03 §4.3 option b): a `Dependencies.Principal` injected dependency** — register a resolver in `_make_dep_resolvers` (`_compile.py`) yielding the per-request principal from `ctx.headers`, so it rides the same proven OBO closure as `ctx.ws` and survives the async→sync thread hop (`_compile.py:182-186`) automatically. The ContextVar alternative (option a) is a smaller diff but has a thread-hop pitfall. **Prototype this before committing E3b** — it's the one sub-task that could slip to L.

Isolation is **row-level by key** (`principal_id` column for memory, `agent_id` for examples) — one shared table partitioned by key, NOT a table per coworker. The MANDATORY isolation test (two OBO principals; A's memory not visible to B; no-principal → no leak) is what converts the design claim into a verified guarantee.

Net-new helpers E3b needs (config can't carry callables): `make_embedding_fn(ws, endpoint_name)` (no built-in embedder exists) and a Lakebase `Engine` builder with the `do_connect` OAuth listener — **first reconcile the `ws.postgres` vs `ws.database` credential-API inconsistency** between `_memory_lakebase.py` and `_session_lakebase.py` against the installed SDK.

## E3c — guards (lowest risk, good first slice)

Scope doc 04. Configurable-as-data: `ToolAllowlist`, `ToolDenylist`, `RateLimit`, `prompt_injection_heuristic`. Out of scope (need live callables): `FeatureFlagGuard`, per-principal rate buckets, Watchdog. **Correctness point:** tool allow/deny must gate on the **`before_tool` hook** (`_callbacks.py` raises `PermissionError`), **not** `input_guardrails` (which see message text only). Schema `[tool.apx.agent.guardrails]` with pydantic `extra="forbid"` so a typo'd guard key fails loud rather than silently disabling protection. Attach at `finalize_agent`, additively (code guards first, then config), idempotent.

## Schema shape (full detail in scope 03 §2 / 04)

```toml
[tool.apx.agent]
name = "sales-coworker"
model = "databricks-claude-sonnet-4-6"
instructions = "Warm, concise."          # persona overlay (E1)
template = { name = "data", catalog = "main", schema = "sales" }   # E3a

[tool.apx.agent.memory]                   # E3b
type = "lakebase"
instance_name = "coworker-lakebase"
database = "agentdb"
embedding_model = "databricks-bge-large-en"
embedding_dim = 1024

[tool.apx.agent.guardrails]               # E3c
blocked_tools = ["delete_record"]
rate_limit = 60
injection_detection = true
```

All three sub-tables require `AgentConfig` to gain typed fields (the loader filters to `AgentConfig.model_fields`, so unknown keys are dropped — same constraint E1/E2 navigated).

## Resolved decisions / recommendations (from scope open questions)

- Memory placement → sub-tables under `[tool.apx.agent]` (keep the family together).
- Session override precedence → explicit `create_app(session_store=...)` arg wins over config.
- Delta write auth → app SP for writes (row-level key still enforces isolation); confirm.
- `validate_at_boot` → default true (deploy-time safety), documented opt-out for offline/locked envs.
- Coexist with the legacy CLI `memory_store` MODULE:VAR key (different surface).
- Coworker `blocked_actions` → `blocked_tools` rename lives in **Coworker's adapter, not the engine** (scope 04).

## Open questions (decide before the relevant sub-plan)

- **Principal threading (E3b Q1):** `Dependencies.Principal` (recommended) vs ContextVar — **prototype first**.
- **`embedding_model` default** (E3b Q2): workspace default vs always explicit.
- **Template-as-config + memory ordering (E3a/E3b):** when both a `template` and a `memory` block are present, the template builds the leaf, then memory tools attach, then persona overlays — confirm this is the right finalize order.
- **Tools-only servability (from E2 / PR #114):** `setup_agent` early-returns ("agent protocol disabled") when there is no `[tool.apx.agent]` section, so a project that configures its agent in **code** (`Agent(name=..., model=...)`) and declares only `[[tool.apx.tools]]` is **not served** — the serve path never reaches `finalize_agent`, so those tools silently don't attach (log/deploy/info paths are unaffected; they finalize unconditionally). Pre-existing behavior, narrow blast radius (the scaffold always emits `[tool.apx.agent]`), but inconsistent with the other 8 chokepoints. Decide: (a) **short-term** — `setup_agent` warns loudly when it disables the protocol while a `[[tool.apx.tools]]` section exists ("declared tools won't serve; add `[tool.apx.agent]`"); (b) **E3 proper** — decouple servability from the config section by synthesizing a minimal config from the agent instance (use `agent`'s own `name` when `[tool.apx.agent]` is absent), so a fully code-configured agent serves and finalizes like every other path. Recommendation: do (a) now-ish (cheap, kills the silent failure), design (b) alongside E3a template-as-config (same "agent definable without a config block" theme).
- Watchdog declarative config — out of scope for E3 (needs live MCP/UC wiring); revisit later.

## Effort

E3a **S–M**, E3b **M** (principal-threading sub-task could reach L — prototype), E3c **S–M (~260 LOC)**. Total ≈ M+ across three sub-plans.

## Dependencies

Hard dependency on **E2's `finalize_agent` chokepoint** (all three E3 surfaces attach there) and on **E1's registry + `apply_config_knobs` persona overlay** (E3a builds from the registry; persona layers via E1). Sequence: E1 ✅ → E2 → E3c/E3a → E3b.
