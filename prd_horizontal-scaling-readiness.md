# PRD: Horizontal-Scaling Readiness for apx Served Databricks Apps

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-09-17

## Summary
Make apx served Databricks Apps safe under horizontal scaling and let scale be **declared**
rather than hand-set. Today conversation/session state defaults to in-memory (per-process);
because Databricks Apps session affinity is best-effort only, a turn landing on a different
replica silently loses history and any mid-turn approval. This work adds a **fail-fast
guardrail** (compile/deploy-time + runtime boot) that turns that silent data-loss footgun
into a caught misconfiguration naming the durable fix, and a **`[tool.apx.agent.deploy]`**
config block that emits `APX_DECLARED_INSTANCES` into the bundle (native Apps
scaling field is de-scoped — SDK `App` has no such field; follow-up #778). Primary success
metric: a scaled + in-memory app fails fast with a fix-naming error at both catch points,
and a scaled + Lakebase app boots clean — proven by a caps reality test.

## Background
apx has three independent state layers, each with a per-process and a shared backend:
ConversationStore (`InMemoryConversationStore` vs `LakebaseConversationStore`), the LangGraph
checkpointer (`InMemorySaver` default vs `PostgresSaver` via Lakebase), and the long-term
MemoryStore. The durable path already exists and ships (#329): `[tool.apx.agent.session]
type='lakebase'` yields `LakebaseConversationStore` + `PostgresSaver`, which span replicas.
`_chat_agent.py` (~305-313) already logs a warning that its `InMemorySaver` default "does not
span replicas." What's missing is (a) any guard that stops a scaled deploy from shipping with
in-memory state, and (b) a declared way to turn scaling on — the deploy emitter
(`_project_gen._build_databricks_yml`, lines 686-807) writes no `instances`/`autoscale` field
today. Databricks Apps horizontal scaling = 1–5 instances behind one URL, best-effort sticky
cookie only, with explicit guidance not to rely on instance-local state.

## Research Inputs
- **Codebase map** (two Explore passes): store/checkpointer backends and resolution
  (`_memory_wiring.py` `resolve_conversation_store`/`resolve_checkpointer`), lifespan wiring
  (`_wiring.py` ~1104-1311), deploy emitter (`_project_gen.py` `_build_databricks_yml`
  686-807, `_build_app_yml` 810-822), config models (`_models.py` `_BackendConfig` 145-162,
  `MemoryBackendConfig` 165-182, `AgentConfig` sub-block registration 469-476).
- **Runtime signal (constraint)**: Databricks Apps exposes **no** replica or worker count to
  the app process. Only `DATABRICKS_APP_PORT` indicates running-on-Apps (`_dev._is_deployed_app`,
  `_dev.py:1198-1205`). Scaffolded apps start uvicorn with **no `--workers`** (single worker
  per replica; `_project_gen.py:262-272`). Therefore the runtime guardrail cannot natively
  detect scale — it reads back a count apx itself emits (decision below).
- **Platform**: Apps horizontal scaling 1–5 instances, best-effort `__Host-databricks-app-router`
  cookie, "do not rely on instance-local state."

## Goals
- G1: A scaled app configured with in-memory session state **fails to compile/deploy** and
  **fails to boot in prod**, with an error naming `[tool.apx.agent.session] type='lakebase'`.
- G2: In local single-process dev, the same in-memory config **boots with a warning**, not a
  failure.
- G3: A scaled app configured with Lakebase boots clean at both catch points.
- G4: Scale is declared once in `[tool.apx.agent.deploy]` and emitted into the Apps bundle;
  no hand-editing of `databricks.yml` for instance count.
- G5: One shared predicate backs both catch points; no new backend, no default change.

## Non-Goals
- A turnkey command that provisions Lakebase for the user.
- Any new storage backend — Lakebase/Managed already exist.
- Changing the default session backend to durable (defaults stay; only *scaled + in-memory*
  is guarded).
- Implementing sticky-session/affinity logic in apx (platform concern; affinity is
  best-effort and must not be relied on).
- Live multi-replica deploy validation against a real workspace (tests use local ASGI +
  mocked SDK, per repo convention).
- Guarding the long-term MemoryStore backend (out of the per-turn continuity path for v1;
  the predicate covers conversation store + checkpointer only).

## Requirements

### Functional
- **FR-1 — Deploy config block.** New `[tool.apx.agent.deploy]` block parsed by a Pydantic
  model inheriting `_BackendConfig` (`extra="forbid"`), registered on `AgentConfig` like
  `session`/`memory` (`_models.py:469-476`). Fields: `instances: StrictInt | None` (valid 1–5) and
  `autoscale: {min: StrictInt, max: StrictInt} | None` (each 1–5, `min <= max`). At most one of
  `instances`/`autoscale` may be set; setting both is a validation error.
- **FR-2 — Shared predicate.** A single helper computes `declared_max_replicas(config)`
  (= `instances`, else `autoscale.max`, else 1) and `session_is_in_memory(config, ws, agent)`
  by reusing `resolve_conversation_store` / `resolve_checkpointer`: true when the conversation
  store resolves to `InMemoryConversationStore` (or `None` when a session is expected) **or**
  the checkpointer resolves to `None` (i.e. LangGraph's in-process `InMemorySaver`, not a
  `PostgresSaver`). Both catch points call this one predicate.
  - **Compile-mode caveat (must handle):** the compile path (`_build_databricks_yml`) runs
    with **no workspace** (`ws=None`); resolving a `type='lakebase'` session needs `ws`, so a
    naive `session_is_in_memory(config, ws=None)` would resolve the store to `None` and
    **falsely report in-memory for a lakebase config** — wrongly blocking scaled+lakebase
    (breaks AC-4). The predicate must treat a **declared** `session.type=='lakebase'` as
    durable-intent regardless of `ws` (check the declared type, or pass a compile-mode flag).
    Runtime (FR-5) must **not** take this shortcut: pass
    `trust_declared_lakebase=False` and use the resolved store/checkpointer. A
    declared lakebase that failed to build is still process-local and must refuse boot.
- **FR-3 — Compile/deploy-time guard.** In the deploy/compile path that builds the bundle
  (`_project_gen._build_databricks_yml`, line 686 region, or the deploy CLI entry that calls
  it), if `declared_max_replicas(config) > 1` **and** `session_is_in_memory(...)`, raise a
  clear error naming the `type='lakebase'` fix; refuse to emit the bundle.
- **FR-4 — Bundle emission (env-only).** When `[tool.apx.agent.deploy]` declares scaling,
  `_build_databricks_yml` emits an `APX_DECLARED_INSTANCES` env var (= `declared_max_replicas`)
  into the `config.env` list (~lines 756-766) for the runtime read-back.
  **De-scoped:** a native Apps horizontal-scaling bundle field is **not** emitted — Databricks
  SDK 0.102.0's `App` model has no scaling field (only `compute_size`, vertical), and the DABs
  `resources.apps` schema mirrors it, so instance count is UI/API-only and cannot be declared
  in `databricks.yml`. Operators set the instance count in the Apps UI (documented in FR-6);
  apx still carries the declared count forward via the env var so the runtime guard works.
  Follow-up issue tracks emitting the native field if/when the SDK/bundle gains one.
- **FR-5 — Runtime boot guard (emit-and-read-back).** In the lifespan wiring
  (`_wiring.py` ~1104-1311), read `APX_DECLARED_INSTANCES` (int, default 1). If `> 1` **and**
  `_dev._is_deployed_app()` (on Apps = prod) **and** `session_is_in_memory(...)` → raise,
  refusing boot with the same fix-naming error. If on Apps + in-memory but declared == 1, keep
  the existing durability warning. If **not** on Apps (local dev), warn-only regardless of the
  declared count. Reuse the `_chat_agent.py` warning site for the warn path.
- **FR-6 — Discoverability.** The guardrail error text (both catch points) names
  `[tool.apx.agent.session] type='lakebase'`. `docs/running/sessions-and-memory.md` and
  `docs/deploy/apps-vs-model-serving.md` gain a "Scaling" section covering the deploy block,
  the durable requirement, the best-effort-affinity caveat, and — because instance count is
  not declarable in the bundle — a note that operators set the instance count in the
  Databricks Apps UI (apx carries the declared count via `APX_DECLARED_INSTANCES` for the
  runtime guard).

### Non-functional
- **NFR-1** — The compile-time and runtime checks add no measurable startup latency (a config
  read + a store-type check that already runs during resolution). No new dependency.
- **NFR-2** — Back-compatible: an app with no `[tool.apx.agent.deploy]` block behaves exactly
  as today (`declared_max_replicas == 1`, no guard fires, no env emitted).

## Design

### Architecture
- **Config** (`_models.py`): add `DeployConfig(_BackendConfig)` + nested `AutoscaleConfig`
  with `@field_validator`s for the 1–5 range and `min <= max`, and a `@model_validator`
  rejecting both `instances` and `autoscale`. Register `deploy: DeployConfig | None` on
  `AgentConfig` mirroring `memory`/`session` (469-476).
- **Predicate** (`_memory_wiring.py`, beside the `resolve_*` functions it reuses): two small
  pure functions `declared_max_replicas(config)` and `session_is_in_memory(config, ws, agent)`,
  plus a combined `scaled_in_memory(...)` used by both guards. Reuse, don't re-derive, the
  existing resolution so backend detection stays in one place.
- **Compile guard + emission** (`_project_gen.py`): call the predicate in `_build_databricks_yml`
  before assembling the resource; on violation raise; otherwise inject
  `APX_DECLARED_INSTANCES` (no native Apps scaling field — see FR-4 / #778).
- **Runtime guard** (`_wiring.py` lifespan): after stores/checkpointer resolve, evaluate the
  predicate against `APX_DECLARED_INSTANCES` + `_is_deployed_app()`; raise or warn per FR-5.

### Interface changes
- New TOML surface:
  ```toml
  [tool.apx.agent.deploy]
  instances = 3            # 1–5   (fixed count)
  # or, instead of instances:
  # [tool.apx.agent.deploy.autoscale]
  # min = 2
  # max = 5
  ```
- New emitted `APX_DECLARED_INSTANCES` env. Operators set the UI instance count to match
  `[tool.apx.agent.deploy]`; UI-only scale is not validated. No change to
  `RemoteDatabricksAgent` or any served-endpoint signature.

### Data model
`DeployConfig`: `instances: StrictInt | None = None`, `autoscale: AutoscaleConfig | None = None`.
`AutoscaleConfig`: `min: StrictInt`, `max: StrictInt`. No persistence; parse-time only.

## Acceptance Criteria

- [ ] **AC-1**: Given a config with `[tool.apx.agent.deploy] instances=2` and an in-memory
  session (no `[tool.apx.agent.session]`, or `type='inmemory'`), when the deploy/compile path
  builds the bundle, then it raises an error whose message contains
  `type='lakebase'` and no `databricks.yml` is emitted.
- [ ] **AC-2**: Given `APX_DECLARED_INSTANCES=2`, `DATABRICKS_APP_PORT` set, and an in-memory
  session, when the app lifespan starts, then it raises (refuses boot) with the fix-naming
  error.
- [ ] **AC-3**: Given an in-memory session with **no** `DATABRICKS_APP_PORT` (local dev), or
  `APX_DECLARED_INSTANCES` unset/1, when the app lifespan starts, then it boots successfully
  and logs a warning (no exception).
- [ ] **AC-4**: Given `[tool.apx.agent.deploy] instances=3` **and** `[tool.apx.agent.session]
  type='lakebase'`, compile emits. Runtime with `APX_DECLARED_INSTANCES=3` +
  `DATABRICKS_APP_PORT` **raises** unless a checkpointer actually resolved (declaration
  alone is not enough). A stubbed resolved store+checkpointer boots clean.
- [ ] **AC-5**: Given `[tool.apx.agent.deploy] instances=3`, when `_build_databricks_yml`
  emits, then the written `databricks.yml` contains an `APX_DECLARED_INSTANCES=3` env entry
  in the app `config.env` list (verified via `ctk.verify(Artifact(path, must_contain=...))`).
  (Native Apps scaling field de-scoped per FR-4 — not asserted.)
- [ ] **AC-6**: Given `[tool.apx.agent.deploy]` with `instances=6` (out of range), or both
  `instances` and `autoscale` set, or `autoscale.min > autoscale.max`, when the config is
  parsed, then a Pydantic validation error is raised.

Machine-verifiable fields are mirrored in the Agent Handoff JSON below.

## Risks
- **Exact Apps bundle scaling key unknown**: the precise `databricks.yml` field name for Apps
  horizontal scaling isn't confirmed in-repo. Mitigation: confirm via the databricks-dabs /
  databricks-apps skill (or `databricks bundle schema`) before implementing FR-4; AC-5 asserts
  on the confirmed key.
- **Predicate false-positive on `None` checkpointer**: `resolve_checkpointer` returns `None`
  for the in-memory case by design (LangGraph injects `InMemorySaver`). Mitigation: the
  predicate treats `None` checkpointer as in-memory, matching `_chat_agent.py`'s own logic;
  AC-4 guards against false-positives on the Lakebase path.
- **Redundancy between catch points**: because compile blocks the declared bad combo,
  `APX_DECLARED_INSTANCES>1 + in-memory` can only reach runtime via a hand-edited bundle.
  That's intended defense-in-depth (drift/out-of-band edits), stated so it isn't mistaken for
  dead code.

## Open Questions
- [x] ~~Confirm the exact Databricks Apps bundle YAML key for horizontal scaling~~ **RESOLVED
  (2026-09-17):** no such key exists — SDK 0.102.0 `App` has only `compute_size` (vertical);
  instance count is UI/API-only. FR-4 de-scoped to env-only; AC-5 asserts the env entry.

## Corrected file references (from Phase A during implementation)
- InMemorySaver warning site: `_chat_agent.py` **~1095-1110** (not 305-313) — FR-5 dev warn reuses it.
- Lifespan resolves `_store` + `_checkpointer` at `_wiring.py` **1101-1134**; FR-5 guard hooks right after 1134.
- `_build_databricks_yml` is a pure f-string, **~690-800**; app `config.env` list at **~756-766** (FR-4 adds an entry there; FR-3 raises before the return).

---

## Agent Handoff

```json
{
  "prd_version": "1.0",
  "goal": "A scaled apx Databricks App fails fast (compile + prod boot) when session state is in-memory, naming the Lakebase fix, and boots clean when Lakebase-backed; scale is declared via [tool.apx.agent.deploy] and emitted into the bundle.",
  "success_criteria": [
    "compile fails on declared instances>1 + in-memory, naming type='lakebase'",
    "prod boot fails on APX_DECLARED_INSTANCES>1 + on-Apps + in-memory",
    "local dev in-memory boots with warning only",
    "scaled + lakebase boots clean at both catch points",
    "databricks.yml carries APX_DECLARED_INSTANCES for a declared deploy block",
    "deploy-block config validation enforces range/exclusivity"
  ],
  "convergence": {
    "stopping_signal": "cd python && uv run pytest tests/gates/ -q",
    "progress_metric": "failing gate-test count",
    "known_ceiling": "RESOLVED: Databricks Apps has no declarable horizontal-scaling bundle field (SDK 0.102.0 App = compute_size only). FR-4 de-scoped to emitting APX_DECLARED_INSTANCES env only; instance count is UI/API-only.",
    "re_represented": false
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "compile/deploy raises naming type='lakebase' when declared instances>1 + in-memory; no bundle emitted", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_hscale_readiness_ac1.py", "gate_test": "test_compile_refuses_scaled_in_memory" },
    { "id": "AC-2", "description": "runtime lifespan raises when APX_DECLARED_INSTANCES>1 + DATABRICKS_APP_PORT set + in-memory", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_hscale_readiness_ac2.py", "gate_test": "test_prod_boot_refuses_scaled_in_memory" },
    { "id": "AC-3", "description": "local dev (no DATABRICKS_APP_PORT) or declared<=1 + in-memory boots with warning, no exception", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_hscale_readiness_ac3.py", "gate_test": "test_dev_boot_warns_only" },
    { "id": "AC-4", "description": "scaled + type='lakebase' compiles; runtime raises unless checkpointer actually resolved", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_hscale_readiness_ac4.py", "gate_test": "test_scaled_lakebase_compile_clean" },
    { "id": "AC-5", "description": "emitted databricks.yml contains APX_DECLARED_INSTANCES env entry for declared deploy block (native scaling field de-scoped)", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_hscale_readiness_ac5.py", "gate_test": "test_bundle_emits_declared_instances_env" },
    { "id": "AC-6", "description": "DeployConfig validation: instances range 1-5, instances/autoscale exclusive, autoscale min<=max", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/gates/test_hscale_readiness_ac6.py", "gate_test": "test_deploy_config_validation" }
  ],
  "must_have": [
    "FR-1 [tool.apx.agent.deploy] DeployConfig(_BackendConfig) with instances(1-5)/autoscale{min,max}, mutually exclusive",
    "FR-2 shared predicate declared_max_replicas + session_is_in_memory reusing resolve_conversation_store/resolve_checkpointer",
    "FR-3 compile/deploy-time guard in _project_gen._build_databricks_yml",
    "FR-4 emit APX_DECLARED_INSTANCES env into databricks.yml config.env (native scaling field de-scoped: not in SDK/bundle)",
    "FR-5 runtime boot guard in _wiring.py lifespan (~after 1134): read APX_DECLARED_INSTANCES + _is_deployed_app + predicate; raise in prod, warn in dev (reuse _chat_agent.py ~1095-1110 warning)",
    "FR-6 error text names type='lakebase'; docs scaling section"
  ],
  "out_of_scope": [
    "turnkey Lakebase-provisioning command",
    "new storage backend",
    "changing default session backend",
    "sticky-session/affinity logic",
    "live multi-replica workspace validation",
    "guarding the long-term MemoryStore backend"
  ],
  "constraints": {
    "tech_stack": "Python, Pydantic v2 config models, FastAPI/uvicorn served app, LangGraph checkpointer, Databricks Asset Bundle (databricks.yml) + app.yml",
    "key_files": [
      "python/src/apx_agent/_models.py:145-182,469-476",
      "python/src/apx_agent/_memory_wiring.py",
      "python/src/apx_agent/_wiring.py:1104-1311",
      "python/src/apx_agent/_project_gen.py:686-822,262-272",
      "python/src/apx_agent/_dev.py:1198-1205",
      "python/src/apx_agent/_chat_agent.py:305-313",
      "python/src/apx_agent/_conversation.py",
      "python/src/apx_agent/_conversation_lakebase.py",
      "python/src/apx_agent/_checkpoint_lakebase.py",
      "python/capabilities.yaml",
      "docs/running/sessions-and-memory.md",
      "docs/deploy/apps-vs-model-serving.md"
    ],
    "patterns": "Config: inherit _BackendConfig (extra='forbid'), field_validator for ranges, model_validator for exclusivity, register on AgentConfig like memory/session. Reuse resolve_conversation_store/resolve_checkpointer for backend detection (do not re-derive). Runtime on-Apps check via _dev._is_deployed_app (DATABRICKS_APP_PORT). Ponytail: one shared predicate, no new backend, no default change. Ctk: reality tests in tests/gates/ + a caps capability whose check IS the gate; assert emitted artifacts with ctk.verify(Artifact(path, must_contain=...)), not .exists().",
    "caps_capability": { "id": "scaled-in-memory-refused", "tier": "cheap", "check": "python/tests/gates/test_hscale_readiness_ac1.py python/tests/gates/test_hscale_readiness_ac2.py python/tests/gates/test_hscale_readiness_ac4.py", "deps": ["python/src/apx_agent/_models.py","python/src/apx_agent/_memory_wiring.py","python/src/apx_agent/_wiring.py","python/src/apx_agent/_project_gen.py"] }
  },
  "preferred_skills": ["databricks-dabs", "databricks-apps"],
  "escalate_on": [
    "resolve_checkpointer/resolve_conversation_store semantics make in-memory detection ambiguous for a backend other than inmemory/lakebase",
    "the compile-mode predicate fix (declared-lakebase = durable when ws=None) cannot be expressed cleanly without changing resolve_* signatures",
    "config-model style cannot express instances/autoscale exclusivity without a new base"
  ],
  "resolved_decisions": [
    "FR-4 de-scoped to env-only (APX_DECLARED_INSTANCES); native Apps scaling field not declarable in bundle (SDK 0.102.0). Operators set instance count in Apps UI; docs cover it.",
    "compile guard must treat declared session.type=='lakebase' as durable regardless of ws (ws=None at compile time), else compile AC-4 breaks",
    "runtime guard must use resolved backends (trust_declared_lakebase=False); declared lakebase + no checkpointer still refuses boot"
  ],
  "loop_guards": {
    "max_iterations": 7,
    "state_hash_check": true,
    "heartbeat_interval_seconds": 30,
    "on_stuck": "pause_and_surface",
    "on_no_progress": "stop_and_escalate",
    "state_persistence": "local_disk"
  }
}
```
