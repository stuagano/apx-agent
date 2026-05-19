# apx-agent Framework Audit — 2026-05-18

> **Status: historical snapshot — superseded by the living gap plan.**
> This audit captures a point-in-time view as of 2026-05-18, when the shortage-intel marriage branch (`feat/shortage-intel-marriage`) forked from main at `c2773d3`. Between then and the merge (`6eabe8a`), main shipped 15 additional commits — callbacks, `@tool` decorator + UC publish, platform tool factories (vector_search/sql/foundation_model), Managed MCP, full `apx` CLI, sessions (InMemory/Delta/Lakebase), MLflow experiments, `publish_to_supervisor`, watchdog integration. Most ADK coverage gaps this audit flagged are now closed on main.
>
> **The living source of truth is `docs/future-work/gap-plan-2026-05-18.md`.** This audit is preserved as the rationale trail for the work merged on `feat/shortage-intel-marriage` and the architectural decisions documented inline. Items below are reconciled against current main state — open items here are open in the gap plan too; resolved items reference the commit that closed them.

## Context

This audit emerged from porting the shortage-intelligence-agent customer code (`Ahkbar/shortage-intelligence-agent-main`) into the apx-agent example (commit `1c84b2a`) and then surfacing derived framework improvements (commit `b6278f7`). The port-by-doing approach exposed real gaps in the framework's primitives, naming, and cross-language consistency. This doc captures what was found — and below, what's already been closed by parallel main work.

**Already shipped on `feat/shortage-intel-marriage`:**
- ✅ Public `decode_statement` helper for `StatementResponse` → `list[dict]`
- ✅ `_build_chat_databricks` strips both `temperature` and `top_p` (GPT-5 family was 400-ing on `top_p`)
- ✅ Updated to new `ChatDatabricks` signature (`stream`, `custom_inputs` kwargs; canonical `model=` over deprecated `endpoint=`)
- ✅ Parity test no longer silently skips on missing core deps
- ✅ Stale `langchain-databricks` references in docstrings updated to `databricks-langchain`
- ✅ `agent_tool` (Python) and `agentTool` (TypeScript) — first-class agent-as-tool composition primitive (Section B.2 below). Handles local in-process AND remote agents transparently. Replaces the deprecated `toSubAgentTool` in TS.
- ✅ **Public `get_llm()` factory + `ChatDatabricksGptReasoning` named subclass (Section A Option 1).** Provider-quirk handling promoted from private compile-path helper to public API. Tool-internal LLM calls (synthesis, classifiers, judges) now get the same protection as the agent's main LLM. Routing by endpoint prefix means Claude/Llama/Gemini honor `temperature` while GPT-5 endpoints get the defense-in-depth strip. Example's `validate_against_market_news` now goes through `get_llm` instead of bypassing into raw `ChatDatabricks`.
- ✅ **`genie_query_tool` / `genieQueryTool` (Section D.1).** Structured-results sibling to `genie_tool`. Returns `{sql_results, result_count, genie_response, generated_sql}` for downstream agents that need to reason over the data, not just read the summary. Python `genie_tool` also modernized to use `ws.genie.*` SDK instead of raw `api_client.do(...)`. Example wires `genie_query_tool` into `historical_agent` so the LLM can do ad-hoc lookups when the canned query returns nothing.

The remaining findings are below, ordered by leverage and reversibility.

---

## A. The biggest open call: cross-language LLM-client divergence

The Python and TypeScript frameworks have substantially diverged at the LLM-client layer. This is the architectural call that affects every future provider/quirk decision.

| Concern | Python | TypeScript |
|---|---|---|
| LLM client | `databricks_langchain.ChatDatabricks` (LangGraph runtime) | Raw `fetch()` to `/serving-endpoints/<model>/invocations` |
| Request body | `_prepare_inputs` builds full payload, optional fields included if set on client | Minimal: `{ model, messages, [tools, tool_choice, stream] }` |
| Provider-quirk handling | Subclass `ChatDatabricks` to *strip* rejected fields (defensive) | Structural — *never includes* fields providers can reject |
| Surface area | LangChain ecosystem (callbacks, prompt templates, retrievers) | None — just chat completions |

**The TS approach is the cleaner mental model.** "Don't send what providers can reject" is structurally safer than "send everything and subclass to strip." Today, Python's `_build_chat_databricks` strips `temperature` + `top_p` (verified GPT-5 quirks) but is *private to the compile path* — every tool that makes its own internal LLM call (e.g., the shortage-intel `validate_against_market_news` synthesis) bypasses it and is exposed.

### Three plausible directions

| Option | What changes | Trade |
|---|---|---|
| **1. Promote Python helper** ✅ **shipped** | Expose `apx_agent.get_llm(endpoint, **kwargs)` factory + `ChatDatabricksGptReasoning` subclass. Routing by endpoint prefix. Tool-internal LLM calls get same protection as compile-path. | Smallest change; preserves LangChain integration. Still per-provider defense, not structural. |
| **2. Align Python to TS — minimal payload** | Stop sending `temperature`/`top_p` at all from `_build_chat_databricks`. Drop subclass. Provider quirks become impossible by construction. | Cleanest. But removes a knob users may expect; agents that want sampling control need a different escape hatch. |
| **3. Aligned `LLMClient` abstraction in both languages** | Define a shared `LLMClient` interface (input shape, output shape, streaming contract). Python wraps `ChatDatabricks`, TS wraps `fetch`. Both expose the same surface to user code. | Biggest investment. Real cross-language consistency. Pays back across every future LLM-touching feature. |

**Option 1 shipped.** `apx_agent.get_llm(endpoint)` is now public. `_compile.py::_build_chat_databricks` delegates to it. The example's `validate_against_market_news` synthesis step uses `get_llm` instead of bypassing into raw `ChatDatabricks`. Routing by endpoint prefix means Claude/Llama/Gemini honor `temperature` (which the old unconditional-strip silently dropped — a correctness bug), and GPT-5 endpoints get the defense.

Notable correctness improvement that fell out: the prior `_build_chat_databricks` stripped `temperature`+`top_p` *unconditionally for every endpoint*. The compile path never actually passed an agent's `temperature` into it, so the strip was moot for the framework's main LLM. But anything that called the helper with temperature (had there been one) would have been silently overridden. The new prefix-routed factory only strips for GPT-5; non-reasoning endpoints honor the caller's setting.

**Option 3 remains the eventual destination** if the framework lives for years. Option 2 stays risky — sampling control is a real expectation.

---

## B. ADK primitive coverage scan

Comparing apx-agent's surface against Google ADK's:

| ADK primitive | apx-agent Python | apx-agent TypeScript | Gap |
|---|---|---|---|
| `LlmAgent` / `Agent` | ✅ | ✅ | — |
| `SequentialAgent` | ✅ | ✅ | — |
| `ParallelAgent` | ✅ | ✅ | — |
| `LoopAgent` | ✅ | ✅ | — |
| `RouterAgent` (LLM-driven routing) | ✅ | ✅ | — |
| `HandoffAgent` (peer handoff) | ✅ | ✅ | — |
| `RemoteAgent` (cross-endpoint sub-agent) | ✅ | ❌ TS calls these `subAgents` config strings | Naming + symmetric primitive |
| `EvolutionaryAgent` | ❌ | ✅ | Python parity needed |
| `HypothesisAgent` | ❌ | ✅ | Python parity needed |
| `ParetoAgent` | ❌ | ✅ | Python parity needed |
| Agent-as-tool (call another agent as a function) | ✅ `agent_tool(agent)` (shipped) | ✅ `agentTool(target)` (shipped) | — |
| Session/state management | ✅ `_session.py` / `_session_delta.py` / `_session_lakebase.py` (`072a71c`, `99cfdd3`) | ⚠️ Express session, no UC durability | TS still lacks UC-durable session store |
| Pre/post model callbacks | ✅ `_callbacks.py::before_model` / `after_model` (`e52b69a`) | ⚠️ trace.ts spans cover some | TS still lacks explicit hooks |
| Pre/post tool callbacks | ✅ `_callbacks.py::before_tool` / `after_tool` (`e52b69a`) | ❌ | TS still lacks |
| Eval framework | ✅ `apx_agent.evaluate` Mosaic AI wrapper (`2336238`) | ✅ eval surface | — |
| CLI (`adk` equivalent) | ✅ `apx` full surface (`f73b965`, `d33b3b0`, `dfa7f53`) | ⚠️ none | TS still has no CLI |
| Multimodal | ❌ | ❌ | Foundation Model API multimodal endpoints supported by ChatDatabricks but no first-class apx-agent surface |
| Streaming | ✅ SSE in dev UI; `/invocations` streams | ✅ same | — |
| A2A protocol | ✅ `.well-known/agent.json` mounted by `create_app` | ✅ same | — |

**Reconciliation against current main:** every "Two coverage gaps with the highest leverage" item from the original audit is closed except the EvolutionaryAgent/HypothesisAgent/ParetoAgent Python port. Pre/post tool callbacks shipped in `e52b69a`. The remaining open gaps are: (a) Python EA primitives parity, (b) TS-side coverage for sessions/callbacks/CLI, and (c) multimodal first-class surface.

**Original open items (now closed by main):**
1. ~~No pre/post tool callbacks.~~ → `e52b69a feat: callbacks — before/after model + fix dead before/after tool surface`. Both sides of the LLM loop now have explicit hooks.

**Remaining:**
1. **Python lacks `EvolutionaryAgent` / `HypothesisAgent` / `ParetoAgent`.** TS has them; Python does not. Worth noting: the TS implementations have pre-existing test failures (see `evolutionary-durable.test.ts`), so the port is blocked on whichever-side-fixes-the-population-store-bug-first.

**B.2 Agent-as-tool — shipped.** Both `agent_tool(agent)` (Python) and `agentTool(target)` (TypeScript) wrap any agent — local or remote — as a tool callable from another `LlmAgent`. Workflow agents compose along deterministic edges; `agent_tool` composes along LLM-driven edges. The Python wrapper takes any `BaseAgent`; the TS wrapper accepts `AgentConfig | AgentExports | string` (URL). Tests in `python/tests/test_agent_tool.py` and `typescript/tests/agent-tool.test.ts`.

---

## C. Naming inconsistencies

| Concept | Python | TypeScript | Recommendation |
|---|---|---|---|
| Leaf agent class | `LlmAgent` (alias: `Agent`) | `createAgentPlugin` (config-driven) | TS doesn't have a class; the symmetry is `Agent` (Python class) vs `createAgentPlugin` (TS factory). Document the asymmetry; don't force matching shapes. |
| Tool definition | Typed function signature OR `@tool` decorator with optional UC sync (`e85b59c`) | `defineTool({...})` | Python now also supports `@tool(uc=...)` for UC-syncable tools; TS has no UC-sync equivalent. Asymmetry now wider, not narrower. |
| Sub-agent reference | `sub_agents=["endpoints/x"]` | `subAgents: ["endpoints/x"]` | Consistent ✅ |
| Dependency injection | `Dependencies.Workspace`, `Dependencies.UserClient`, `Dependencies.Sql` | (no equivalent; `getRequestContext()`) | The most consequential gap. Python tools express deps via type annotations; TS tools use side-channel context. Either lift TS to type-annotation injection or document the asymmetry as deliberate. |
| Tool factory naming | `genie_tool`, `lineage_tool`, `schema_tool`, `catalog_tool`, `uc_function_tool` | `genieTool`, `lineageTool`, ... | Consistent (snake/camel by convention) ✅ |
| Compile entrypoint | `compile_to_chat_agent`, `compile_to_langgraph`, `log_agent` | `compileToChatAgent` | Consistent ✅ |

The Python `Dependencies.*` system is the most distinctive bit of the framework. It's not represented at all in TS. Either it's a Python-only ergonomic (document loudly) or it should be ported (substantial new TS work). Either is fine — choose deliberately.

---

## D. Deferred from the port

Three items I noted during the marriage but didn't ship in Phase A:

### D.1 Ad-hoc Genie tool factory — ✅ shipped

`genie_query_tool(space_id)` (Python) and `genieQueryTool(spaceId)` (TypeScript) return structured results — `sql_results`, `generated_sql`, `genie_response` — instead of just the narrative text answer. Used by the shortage-intel `historical_agent` so the LLM can fall back to ad-hoc Genie exploration when the canned `find_historical_patterns` query returns nothing. Python `genie_tool` was also modernized in the same pass to use the `ws.genie.*` SDK rather than raw `api_client.do(...)` HTTP calls.

### D.2 Migration note: the obsolete `core/__init__.py` override

The old shortage-intel example had a project-local `Dependencies.Workspace` patch (`backend/core/__init__.py`) because the framework's default didn't handle Databricks Apps OBO correctly. **The framework now handles this natively** (see `_defaults._make_workspace_client`). Existing apx-agent users carrying the override can delete it.

This belongs in:
- The framework's CHANGELOG when a release is cut
- A migration note in the docs (probably `docs/superpowers/migrations/`)

### D.3 The shortage-intelligence-agent customer fork

`Ahkbar/shortage-intelligence-agent-main` is the customer's hand-rolled version that the marriage consolidated. It's still on disk, still being edited (we just spent today's earlier turns improving it). Options:
1. **Delete it** — the canonical version now lives in apx-agent/python/examples/. Loss: the "before" snapshot is only in git history.
2. **Convert it to a thin import** — `from apx_agent ...` + a tiny project-specific config layer. Customer keeps owning their repo; framework stays the single source of truth.
3. **Leave both, accept drift** — costs maintenance, breaks the marriage premise.

Option 2 is the structurally right move if Rand (the customer) is going to keep iterating on the deployment specifics. Option 1 is cleaner if the customer is happy to upstream and consume from `python/examples/`.

**Status: still open as of merge.** Customer fork not yet consolidated.

---

## E. Smaller observations

| Finding | Severity | Action |
|---|---|---|
| `_compile.py::_build_chat_databricks` was private but used everywhere implicitly. The provider-quirk defense should be a public, named pattern users can apply to tool-internal LLM calls. | ✅ Resolved | Shipped via `apx_agent.get_llm` (Section A Option 1). `_build_chat_databricks` now delegates. |
| Framework's `python/pyproject.toml` lists `langgraph` both as core dep AND in optional `[project.optional-dependencies].langgraph`. Confusing. Pick one. | Low | Remove from optional |
| Example's `pyproject.toml` had `apx-agent = { path = "../../src", editable = true }` — wrong path (apx-agent's pyproject is at `../../`, not `../../src`). | Low | Fixed in marriage commit |
| `_metadata.py` is referenced in `[tool.apx.metadata]` but I didn't verify it exists / is generated. Untracked file? Bootstrap artifact? | Low | Audit |
| Parity test depends on a hardcoded absolute path (`/Users/stuart.gano/Documents/Ahkbar/...`) to the customer repo. Fine for the author, broken for anyone else. | Medium | Make path configurable via env var or skip with explanation |
| Multiple example directories on disk with similar names (`shortage_intelligence/`, `shortage-intelligence-agent/`, `shortage_intelligence_compile_demo.py`). Easy to confuse. | Low | Consolidate or label clearly |

---

## Recommended next moves (if continuing)

1. ~~**Pick the cross-language LLM-client direction**~~ — Option 1 shipped (`cf9a94a`). Option 3 (shared `LLMClient` abstraction) remains the eventual destination.
2. ~~**Promote `_build_chat_databricks` to public `get_llm()`**~~ — shipped (`cf9a94a`).
3. **Decide on the Python ↔ TS Dependencies system** (Section C) — **still open**. Python now has more depending-injection surface (`@tool(uc=...)` rejects Dependencies params, etc.), widening the TS gap.
4. ~~**Ship `query_genie_tool` factory**~~ — shipped (`19eadf1`).
5. **Customer fork consolidation** (Section D.3) — **still open**. Relationship/handoff question.
6. **CLI parity audit** — superseded; full `apx` CLI shipped on main (`f73b965`, `d33b3b0`, `dfa7f53`). TS still has no CLI.

---

## Status reconciliation summary (2026-05-19)

Items closed since this audit was written:

| Audit item | Closed by |
|---|---|
| Section A.1 — public `get_llm()` factory | `cf9a94a` (this branch) |
| Section B — pre/post tool callbacks | `e52b69a` (parallel main) |
| Section B — pre/post model callbacks | `e52b69a` (parallel main) |
| Section B — session/state UC durability | `072a71c` + `99cfdd3` (parallel main) |
| Section B — Mosaic AI eval wrapper | `2336238` (parallel main) |
| Section B — CLI (full surface) | `f73b965`, `d33b3b0`, `dfa7f53` (parallel main) |
| Section B — agent-as-tool primitive | `61d1ea8` (this branch) |
| Section C — `@tool` decorator surface | `e85b59c` (parallel main) |
| Section D.1 — ad-hoc Genie tool factory | `19eadf1` (this branch) |
| Section D.2 — `core/__init__.py` migration | `1c84b2a` (this branch deletes it; example users can follow) |
| Section E — `_build_chat_databricks` public-factory promotion | `cf9a94a` (this branch) |
| Section E — example pyproject editable-path fix | `1c84b2a` (this branch) |

Items still open:

- **Section A** — full cross-language `LLMClient` abstraction (Option 3). Long-term direction, not urgent.
- **Section B** — Python `EvolutionaryAgent` / `HypothesisAgent` / `ParetoAgent` parity with TS (TS implementations have pre-existing test failures, so blocked).
- **Section B** — multimodal first-class surface.
- **Section B (cross-language)** — TS still lacks session/callback/CLI parity with Python.
- **Section C** — Python-only `Dependencies.*` system; deliberate decision needed.
- **Section D.3** — customer fork consolidation.
- **Section E** — pyproject langgraph dep duplication, `_metadata.py` audit, hardcoded parity-test path, example directory naming cleanup.
- **New observation (post-merge)**: the `agent_tool` primitive from this branch is genuinely new (not redundant with `@tool` decorator, callbacks, or `publish_to_supervisor` — different concerns each) but is **not yet documented in the gap-plan or README**. Worth folding into the framework's composition story alongside Sequential/Parallel/Loop/Router/Handoff/Remote.

The living source of truth for what's next is `docs/future-work/gap-plan-2026-05-18.md`.
