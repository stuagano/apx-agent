# PRD: apx-agent → Agent Bricks Knowledge Assistant OBO Interop

**Version**: 1.0 | **Status**: Draft | **Date**: 2026-08-12

## Summary
Add a first-class apx-agent tool that wraps a Databricks Agent Bricks **Knowledge Assistant (KA)** Model Serving endpoint and invokes it **on behalf of the logged-in user (OBO)** from inside a deployed FastAPI Databricks App. The tool follows the existing endpoint-tool pattern (`foundation_model.py`), so it inherits apx-agent's already-built OBO plumbing (`UserClientDependency`) for free, is wired as a stage in a `SequentialAgent` flow, and returns a grounded-result contract (answer text + ≥1 citation with a source `doc_uri`). Killer demo: a sequential flow whose first stage is a KA grounded on SEC 10-K filings, feeding a grounded, cited answer to the next stage. **Primary success metric: failing gate-test count → 0** (unit gates always; the one live integration gate when Phase 0 + fe-stable user-authorization are available).

## Background
**What exists today.** apx-agent already ships Databricks-endpoint tools as module-level factories — `genie_tool`/`genie_query_tool` (`python/src/apx_agent/genie.py`), `vector_search_tool` (`vector_search.py`), `foundation_model_tool` (`foundation_model.py`), SQL tools (`sql_tools.py`). Every one uses the same shape: a factory `X_tool(endpoint_id, *, name, description)` returning an inner `async def _fn(arg: str, ws: UserClientDependency) -> ...` registered via `build_tool(fn, name=…, description=…, resources=[ResourceSpec("serving_endpoint", …)])`. The OBO path is **already implemented**: `UserClientDependency` (`_defaults.py:231`) resolves via `Depends(_get_user_client)` → `_obo_ws_from_headers`, which reads `X-Forwarded-Access-Token`, **fails closed in the Apps runtime** when no token is present, and falls back to CLI-configured credentials only in local dev. Subagent composition exists via `agent_tool()` (`_agent_tool.py`) and workflow agents including `SequentialAgent` (`_agents.py:611`). Apps packaging/deploy machinery exists (`_apps_registry.py`, `_apps_discovery.py`, `_hot_swap_apps.py`), and serving endpoints are already enumerated at `{host}/serving-endpoints/{name}/invocations` (`_workspace_apis.py`). Read-after-write is governed by `python/capabilities.yaml` + `tests/test_*_reality_ctk.py` + `checks/prove_*.py`.

**What's missing.** No KA tool — nothing wraps an Agent Bricks Knowledge Assistant serving endpoint, and there is no declared capability for "apx-agent invokes the KA endpoint as the user." Everything else (OBO, sequential flows, resource declaration, caps/ctk) is reusable as-is; this PRD adds one tool file, its subagent wiring in a demo flow, tests, and a capability.

**Killer demo.** A `SequentialAgent` where stage 1 is an `LlmAgent(tools=[knowledge_assistant_tool(<10-K KA endpoint>)])`; it returns a grounded answer + 10-K citation that stage 2 consumes.

## Research Inputs
- **databricks-apps skill** (OBO/serving pattern), confirmed against `_defaults.py`:
  - App declares `user_api_scopes: [serving.serving-endpoints]` and a `serving_endpoint` resource with `permission: CAN_QUERY` in `databricks.yml`.
  - Per request, the runtime injects `X-Forwarded-Access-Token`; apx-agent's `UserClientDependency` already builds the user-scoped `WorkspaceClient` from it and **fails closed in Apps** when absent. SP creds (`DATABRICKS_CLIENT_ID`/`SECRET`) are the background/non-request fallback (local dev / CLI), with `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true` as the explicit opt-in already used by Discover (`_ws_prefer_obo`).
  - **Gotcha:** OBO scopes are wiped by destructive app updates — re-apply after each deploy.
- **databricks-agent-bricks skill** (KA creation + invocation): a KA is created via `databricks knowledge-assistants create-knowledge-assistant` (+ `create-knowledge-source` files source + `sync` → `ONLINE`) and exposes a **standard Model Serving endpoint**. It is queried through the serving invocations API the same way `foundation_model_tool` queries a model (`ws.serving_endpoints.query(name=…, messages=[ChatMessage(...)])`); the KA response carries answer text plus citation records.

## Goals
- A `knowledge_assistant_tool(endpoint_name, *, name, description)` factory in `python/src/apx_agent/knowledge_assistant.py` matching the `foundation_model.py` pattern exactly (inner `async _fn(question, ws: UserClientDependency)`, `build_tool(..., resources=[ResourceSpec("serving_endpoint", endpoint_name)])`).
- The tool runs under OBO with no new auth code — reusing `UserClientDependency`.
- The tool returns a grounded-result dict: `{"answer": str, "citations": [{"doc_uri": str, ...}], ...}` with ≥1 citation when the KA grounds an answer.
- The KA tool is wireable as a `SequentialAgent` stage and its grounded output flows to the next stage.
- Endpoint name is supplied via config/env (never hardcoded).
- A declared capability + reality check for "apx-agent invokes the KA endpoint as the user," plus unit tests that are always runnable (mocked HTTP) and one live-gated integration test.

## Non-Goals
- Building the SEC 10-K Knowledge Assistant itself (**Phase 0 prerequisite**, done interactively; this PRD consumes only the resulting endpoint name).
- Multi-agent supervisor / Direction B (apx-agent stays the orchestrator; the KA is a wrapped tool).
- AppKit (explicitly rejected — keep apx-agent's Python engine).
- Row-level / per-document data governance (corpus is public 10-Ks; OBO here proves identity attribution + invocation authorization, not data-level filtering).
- Frontend/UI polish beyond a minimal chat/flow surface.

## Requirements
### Functional
- **FR-1 — KA serving-endpoint tool.** New factory `knowledge_assistant_tool(endpoint_name, *, name="ask_knowledge_assistant", description=None)` in `python/src/apx_agent/knowledge_assistant.py`, exported from `python/src/apx_agent/__init__.py` alongside the sibling tools. Inner `async def _fn(question: str, ws: UserClientDependency)` calls the KA serving endpoint via `ws.serving_endpoints.query(name=endpoint_name, messages=[ChatMessage(role=USER, content=question)])`; registered via `build_tool(..., resources=[ResourceSpec("serving_endpoint", endpoint_name)])`.
- **FR-2 — OBO extraction (reuse, don't reinvent).** The tool declares `ws: UserClientDependency`; the existing `_get_user_client`/`_obo_ws_from_headers` path (`_defaults.py`) reads `X-Forwarded-Access-Token` and builds the user-scoped client. No new header parsing.
- **FR-3 — SP fallback / fail-closed policy.** Inherit the framework decision: **fail closed in the Apps runtime** when no user token is present; fall back to CLI/SP creds only in local dev; `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true` is the explicit opt-in. No new policy code.
- **FR-4 — Grounded-result contract.** The tool parses the KA response into `{"answer": str, "citations": [{"doc_uri": str, ...}], "question": str}`. On KA/API failure it degrades to `{"error": …, "question": …}` (matching `genie_query_tool`) rather than raising a pipeline-fatal 500.
- **FR-5 — Subagent wiring in a sequential flow.** The KA tool composes into an `LlmAgent(tools=[knowledge_assistant_tool(...)])` used as a `SequentialAgent` stage; the grounded output is passed to the next stage.
- **FR-6 — Endpoint name via config/env.** `endpoint_name` is a factory argument (like `space_id`/`index_name`/`endpoint_name` in siblings). The demo reads it from env (e.g. `APX_KA_ENDPOINT_NAME`) / `[tool.apx.agent]` config — never hardcoded.
- **FR-7 — Capability + reality check.** Add a `capabilities.yaml` entry (`ka-obo-invocation`) with a cheap (mocked, always-run) reality check and a live check (`checks/prove_ka_obo.py`, tier: live) for the fe-stable integration.

### Non-functional
- **NFR-1 — Timeout under the Apps proxy limit.** KA invocation must complete (or fail gracefully) well under the Databricks Apps **120s proxy timeout**; set a bounded client/query timeout (target ≤ 90s) and return a clear error string on timeout rather than hanging the turn.
- **NFR-2 — Graceful failure messaging.** Any KA/serving error yields an actionable string/`{"error": …}` (endpoint not found, not `ONLINE`, scope missing, token absent) — never an unhandled 500. Missing `serving.serving-endpoints` scope surfaces as a fail-closed authorization error.
- **NFR-3 — No new dependencies.** Reuse Databricks SDK `WorkspaceClient` + existing `build_tool`/`ResourceSpec`/`ToolError` primitives; add no packages.

## Design
### Architecture
The KA tool is the fourth Databricks-endpoint tool, structurally identical to `foundation_model_tool`:
- **File:** `python/src/apx_agent/knowledge_assistant.py` (mirrors `foundation_model.py`; do **not** use `from __future__ import annotations`, so `UserClientDependency` resolves eagerly per the sibling files' comments).
- **OBO:** by declaring `ws: UserClientDependency`, the tool runs on the caller's token through `_get_user_client` (`_defaults.py`). No app-layer code is added — apx-agent's FastAPI bootstrap (`bootstrap.py`) + dependency injection already provide the per-request user-scoped client and the fail-closed behavior (`_defaults.py:179-186`).
- **Subagent:** wire into `SequentialAgent` (`_agents.py:611`) as a stage's tool. `agent_tool()` (`_agent_tool.py`) is available if the KA should instead be an LLM-driven delegate, but the sequential demo just registers the tool on the first stage's `LlmAgent`.
- **Resource declaration:** `ResourceSpec("serving_endpoint", endpoint_name)` (same as `foundation_model_tool`) so the platform mints a scoped token and governance/cost flow through the Gateway; this is what ties into the `serving_endpoint` resource + `CAN_QUERY` grant declared in `databricks.yml`.

### Interface changes
- **New tool:** `knowledge_assistant_tool(endpoint_name: str, *, name: str = "ask_knowledge_assistant", description: str | None = None) -> Any` — exported from `python/src/apx_agent/__init__.py`.
- **Config/env key:** `APX_KA_ENDPOINT_NAME` (demo/config supplies it; `[tool.apx.agent]` may declare it). Never hardcoded in the tool.
- **`databricks.yml` additions** (app resource declaration, per databricks-apps skill):
  - `user_api_scopes: [serving.serving-endpoints]`
  - a `serving_endpoint` resource pointing at the KA endpoint with `permission: CAN_QUERY`.
- **Post-deploy step:** re-apply OBO scopes after any destructive app update (documented gotcha).

### Data model
Grounded-result shape returned by the tool:
```json
{
  "question": "…",
  "answer": "…grounded narrative…",
  "citations": [
    { "doc_uri": "…/AAPL-10-K-2023.pdf", "chunk_id": "…", "text": "…snippet…" }
  ]
}
```
Failure shape: `{"question": "…", "error": "…"}`.

## Acceptance Criteria
- [ ] **AC-1 (unit).** *Given* an incoming request carrying `X-Forwarded-Access-Token`, *When* the KA tool resolves its `ws: UserClientDependency`, *Then* the resolved `WorkspaceClient` is built from the **user token** (not SP creds). Verifiable: true. test_type: pytest. gate_file: `python/tests/test_knowledge_assistant.py`. gate_test: `test_obo_uses_user_token`.
- [ ] **AC-2 (unit).** *Given* no forwarded token in a background/non-request context, *When* the client is resolved, *Then* it **falls back to CLI/SP creds in local dev and fails closed in the Apps runtime** (the framework decision in `_get_user_client`; SP fallback only under `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK`). Verifiable: true. test_type: pytest. gate_file: `python/tests/test_knowledge_assistant.py`. gate_test: `test_no_token_fallback`.
- [ ] **AC-3 (unit).** *Given* a mocked KA `serving_endpoints.query` response, *When* the tool parses it, *Then* it returns `answer` text + a `citations` list where each entry has a source `doc_uri`; and a KA error degrades to `{"error": …}` not a raise. Verifiable: true. test_type: pytest. gate_file: `python/tests/test_knowledge_assistant.py`. gate_test: `test_ka_response_parsing`.
- [ ] **AC-4 (unit).** *Given* the KA tool registered on an `LlmAgent` that is a `SequentialAgent` stage (KA `query` mocked), *When* the flow runs, *Then* the KA stage is invoked and its grounded output is passed to the next stage. Verifiable: true. test_type: pytest. gate_file: `python/tests/test_knowledge_assistant.py`. gate_test: `test_ka_subagent_in_flow`.
- [ ] **AC-5 (integration, gated).** *Given* the live fe-stable KA on 10-Ks and OBO user-authorization enabled, *When* queried via the tool as the user, *Then* a non-empty answer + ≥1 citation referencing a 10-K doc is returned, attributed to the user token; absent the `serving.serving-endpoints` scope it fails closed. Verifiable: true. test_type: pytest. gate_file: `python/tests/test_ka_obo_reality_ctk.py`. gate_test: `test_live_ka_grounded_obo`. **Skip/gate reason:** requires Phase 0 KA deployed + fe-stable user authorization (OBO Public Preview). Live capability check: `python/checks/prove_ka_obo.py` (tier: live in `capabilities.yaml`).

## Risks
- **fe-stable user authorization (OBO Public Preview) not enabled** → the live AC returns `user token passthrough not enabled`. *Mitigation:* verify with a workspace admin before running AC-5; unit ACs (AC-1..4) unblock the loop meanwhile.
- **OBO scopes wiped by a destructive app update** → invocations 403 after redeploy. *Mitigation:* documented re-apply-scopes post-deploy step; deploy runbook note.
- **KA cold-start / 120s Apps proxy timeout** → hung turn. *Mitigation:* bounded query timeout (NFR-1), keep prompts small, return a clear timeout error.
- **KA response schema differs from the mocked assumption** (citation field names) → parser mismatch. *Mitigation:* parse defensively; confirm real shape against the live endpoint during AC-5 and adjust the mock to match (escalate if materially different).

## Open Questions
- [ ] Confirm the KA invocations response citation field names/shape (`doc_uri` vs `source`/`url`) against the live fe-stable endpoint; the mock in AC-3 encodes the current assumption and is the single place to correct. (Fail-closed vs SP-fallback is **already resolved** by the framework — see FR-3 — so it is not an open question.)

---

## Agent Handoff
```json
{
  "prd_version": "1.0",
  "goal": "apx-agent, deployed as a FastAPI Databricks App, invokes an Agent Bricks KA serving endpoint on behalf of the logged-in user and returns a grounded answer with >=1 citation, wired as a subagent in a sequential flow, proven by pytest.",
  "success_criteria": ["OBO user-token invocation", "grounded answer + citation contract", "KA subagent runs in a sequential flow"],
  "convergence": {
    "stopping_signal": "pytest (unit gates always; integration gate when live KA + fe-stable user-auth available)",
    "progress_metric": "failing gate-test count",
    "known_ceiling": "live integration AC unreachable until Phase 0 KA is deployed and fe-stable user authorization (OBO Public Preview) is enabled",
    "re_represented": true
  },
  "acceptance_criteria": [
    { "id": "AC-1", "description": "OBO header -> user-scoped client", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_knowledge_assistant.py", "gate_test": "test_obo_uses_user_token" },
    { "id": "AC-2", "description": "no token -> SP fallback (local) / fail-closed (Apps) per framework decision", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_knowledge_assistant.py", "gate_test": "test_no_token_fallback" },
    { "id": "AC-3", "description": "parse KA response -> answer + citations[].doc_uri; error degrades not raises", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_knowledge_assistant.py", "gate_test": "test_ka_response_parsing" },
    { "id": "AC-4", "description": "KA subagent runs in sequential flow, output feeds next stage", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_knowledge_assistant.py", "gate_test": "test_ka_subagent_in_flow" },
    { "id": "AC-5", "description": "live KA on 10-Ks returns grounded cited answer as user; fails closed without serving scope", "verifiable": true, "test_type": "pytest", "gate_file": "python/tests/test_ka_obo_reality_ctk.py", "gate_test": "test_live_ka_grounded_obo", "skip_reason": "gated on Phase 0 KA + fe-stable user authorization" }
  ],
  "must_have": ["KA serving-endpoint tool following foundation_model.py/genie.py pattern", "OBO via existing UserClientDependency (no new auth code)", "subagent wiring in a SequentialAgent flow", "grounded-result contract (answer + citations[].doc_uri)"],
  "out_of_scope": ["building the SEC 10-K KA (Phase 0)", "Direction B / supervisor agent", "AppKit", "row-level data governance", "UI polish"],
  "constraints": {
    "tech_stack": "Python, FastAPI, Databricks SDK (WorkspaceClient), pytest",
    "key_files": ["python/src/apx_agent/knowledge_assistant.py", "python/src/apx_agent/foundation_model.py", "python/src/apx_agent/genie.py", "python/src/apx_agent/vector_search.py", "python/src/apx_agent/_agent_tool.py", "python/src/apx_agent/_agents.py", "python/src/apx_agent/_defaults.py", "python/src/apx_agent/_workspace_apis.py", "python/src/apx_agent/bootstrap.py", "python/src/apx_agent/__init__.py", "python/capabilities.yaml", "python/tests/conftest.py", "python/tests/test_knowledge_assistant.py", "python/tests/test_ka_obo_reality_ctk.py", "python/checks/prove_ka_obo.py"],
    "patterns": "follow foundation_model_tool exactly (factory + inner async _fn(question, ws: UserClientDependency) + build_tool(resources=[ResourceSpec('serving_endpoint', name)])); OBO is already implemented in _defaults.py _get_user_client (fail-closed in Apps, CLI/SP fallback local, APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK opt-in) so declare ws: UserClientDependency and add no auth code; wire as a SequentialAgent stage; inject endpoint name via APX_KA_ENDPOINT_NAME env/config, never hardcode; add a capabilities.yaml entry (cheap mocked reality check + live checks/prove_ka_obo.py)"
  },
  "preferred_skills": ["databricks-apps", "databricks-agent-bricks"],
  "escalate_on": [
    "existing tool/subagent registration pattern is ambiguous or inconsistent across genie.py/foundation_model.py/vector_search.py",
    "no clear per-request context to read x-forwarded-access-token in the current app bootstrap",
    "KA invocations request/response schema differs materially from the foundation_model.py serving_endpoints.query assumption",
    "architectural decision not covered in this PRD"
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
