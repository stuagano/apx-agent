# apx-agent Framework Audit — 2026-05-18

## Context

This audit emerged from porting the shortage-intelligence-agent customer code (`Ahkbar/shortage-intelligence-agent-main`) into the apx-agent example (commit `1c84b2a`) and then surfacing derived framework improvements (commit `b6278f7`). The port-by-doing approach exposed real gaps in the framework's primitives, naming, and cross-language consistency. This doc captures what's left.

**Already shipped on `feat/shortage-intel-marriage`:**
- ✅ Public `decode_statement` helper for `StatementResponse` → `list[dict]`
- ✅ `_build_chat_databricks` strips both `temperature` and `top_p` (GPT-5 family was 400-ing on `top_p`)
- ✅ Updated to new `ChatDatabricks` signature (`stream`, `custom_inputs` kwargs; canonical `model=` over deprecated `endpoint=`)
- ✅ Parity test no longer silently skips on missing core deps
- ✅ Stale `langchain-databricks` references in docstrings updated to `databricks-langchain`
- ✅ `agent_tool` (Python) and `agentTool` (TypeScript) — first-class agent-as-tool composition primitive (Section B.2 below). Handles local in-process AND remote agents transparently. Replaces the deprecated `toSubAgentTool` in TS.

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
| **1. Promote Python helper** | Expose `apx_agent.get_llm(endpoint, **kwargs)` factory + `ChatDatabricksGptReasoning` subclass. Routing by endpoint prefix. Tool-internal LLM calls get same protection as compile-path. | Smallest change; preserves LangChain integration. Still per-provider defense, not structural. |
| **2. Align Python to TS — minimal payload** | Stop sending `temperature`/`top_p` at all from `_build_chat_databricks`. Drop subclass. Provider quirks become impossible by construction. | Cleanest. But removes a knob users may expect; agents that want sampling control need a different escape hatch. |
| **3. Aligned `LLMClient` abstraction in both languages** | Define a shared `LLMClient` interface (input shape, output shape, streaming contract). Python wraps `ChatDatabricks`, TS wraps `fetch`. Both expose the same surface to user code. | Biggest investment. Real cross-language consistency. Pays back across every future LLM-touching feature. |

My read: **Option 1 is the right next step** (small, validated by today's work, ships defense to tool-internal LLM calls), with **Option 3 as the eventual destination** if the framework lives for years. Option 2 is risky — sampling control is a real expectation, and silently dropping it surprises users.

**This is a real architectural decision and deserves explicit choice.**

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
| Session/state management | ⚠️ LangGraph state, no UC durability | ⚠️ Express session, no UC durability | Common backing store for session continuity |
| Pre/post model callbacks | ⚠️ MLflow autolog covers some | ⚠️ trace.ts spans cover some | No explicit `BeforeModelHook`/`AfterModelHook` |
| Pre/post tool callbacks | ❌ | ❌ | Both sides lack |
| Eval framework | ✅ `app_predict_fn` → Agent Eval | ✅ eval surface | — |
| CLI (`adk` equivalent) | ⚠️ `apx` scaffold/dev exists | ⚠️ same | Audit CLI command parity separately |
| Multimodal | ❌ | ❌ | Foundation Model API multimodal endpoints supported by ChatDatabricks but no first-class apx-agent surface |
| Streaming | ✅ SSE in dev UI; `/invocations` streams | ✅ same | — |
| A2A protocol | ✅ `.well-known/agent.json` mounted by `create_app` | ✅ same | — |

**Two coverage gaps with the highest leverage** (post-agent_tool):
1. **Python lacks EvolutionaryAgent / HypothesisAgent / ParetoAgent.** TS has them; Python does not. If population-based search is a real apx-agent feature, it should be a peer in both languages.
2. **No pre/post tool callbacks.** ADK has `before_tool_callback` / `after_tool_callback` for guardrails, logging, redaction. Adding hooks here unlocks security-review patterns without modifying agent code.

**B.2 Agent-as-tool — shipped.** Both `agent_tool(agent)` (Python) and `agentTool(target)` (TypeScript) wrap any agent — local or remote — as a tool callable from another `LlmAgent`. Workflow agents compose along deterministic edges; `agent_tool` composes along LLM-driven edges. The Python wrapper takes any `BaseAgent`; the TS wrapper accepts `AgentConfig | AgentExports | string` (URL). Tests in `python/tests/test_agent_tool.py` and `typescript/tests/agent-tool.test.ts`.

---

## C. Naming inconsistencies

| Concept | Python | TypeScript | Recommendation |
|---|---|---|---|
| Leaf agent class | `LlmAgent` (alias: `Agent`) | `createAgentPlugin` (config-driven) | TS doesn't have a class; the symmetry is `Agent` (Python class) vs `createAgentPlugin` (TS factory). Document the asymmetry; don't force matching shapes. |
| Tool definition | Typed function signature (no decorator) | `defineTool({...})` | Asymmetric on purpose — Python relies on type hints, TS uses Zod. Both work. Document. |
| Sub-agent reference | `sub_agents=["endpoints/x"]` | `subAgents: ["endpoints/x"]` | Consistent ✅ |
| Dependency injection | `Dependencies.Workspace`, `Dependencies.UserClient`, `Dependencies.Sql` | (no equivalent; `getRequestContext()`) | The most consequential gap. Python tools express deps via type annotations; TS tools use side-channel context. Either lift TS to type-annotation injection or document the asymmetry as deliberate. |
| Tool factory naming | `genie_tool`, `lineage_tool`, `schema_tool`, `catalog_tool`, `uc_function_tool` | `genieTool`, `lineageTool`, ... | Consistent (snake/camel by convention) ✅ |
| Compile entrypoint | `compile_to_chat_agent`, `compile_to_langgraph`, `log_agent` | `compileToChatAgent` | Consistent ✅ |

The Python `Dependencies.*` system is the most distinctive bit of the framework. It's not represented at all in TS. Either it's a Python-only ergonomic (document loudly) or it should be ported (substantial new TS work). Either is fine — choose deliberately.

---

## D. Deferred from the port

Three items I noted during the marriage but didn't ship in Phase A:

### D.1 Ad-hoc Genie tool factory

The existing `genie_tool(space_id, description=...)` is for a *fixed* Genie space with a pre-set description. The shortage-intel example also wants a free-form `query_genie(question)` for ad-hoc exploration — every agent rebuilds this. A `query_genie_tool(space_id)` factory would let agents do:

```python
agent = Agent(tools=[query_genie_tool("space-abc"), ...])
```

…and the LLM picks the question text per call. Small, useful, low risk.

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

---

## E. Smaller observations

| Finding | Severity | Action |
|---|---|---|
| `_compile.py:193-213` (`_build_chat_databricks`) is private but used everywhere implicitly. The provider-quirk defense should be a public, named pattern users can apply to tool-internal LLM calls. | Medium | Resolved by Option A.1 above |
| Framework's `python/pyproject.toml` lists `langgraph` both as core dep AND in optional `[project.optional-dependencies].langgraph`. Confusing. Pick one. | Low | Remove from optional |
| Example's `pyproject.toml` had `apx-agent = { path = "../../src", editable = true }` — wrong path (apx-agent's pyproject is at `../../`, not `../../src`). | Low | Fixed in marriage commit |
| `_metadata.py` is referenced in `[tool.apx.metadata]` but I didn't verify it exists / is generated. Untracked file? Bootstrap artifact? | Low | Audit |
| Parity test depends on a hardcoded absolute path (`/Users/stuart.gano/Documents/Ahkbar/...`) to the customer repo. Fine for the author, broken for anyone else. | Medium | Make path configurable via env var or skip with explanation |
| Multiple example directories on disk with similar names (`shortage_intelligence/`, `shortage-intelligence-agent/`, `shortage_intelligence_compile_demo.py`). Easy to confuse. | Low | Consolidate or label clearly |

---

## Recommended next moves (if continuing)

1. **Pick the cross-language LLM-client direction** (Section A, Options 1/2/3). Most consequential decision; everything else cascades.
2. **Promote `_build_chat_databricks` to public `get_llm()`** (Option A.1) regardless of the bigger decision — small unblock, immediate value.
3. **Decide on the Python ↔ TS Dependencies system** (Section C). Either document the asymmetry as intentional or schedule the TS work.
4. **Ship `query_genie_tool` factory** (Section D.1). One file, real ergonomic win.
5. **Customer fork consolidation** (Section D.3). Wait until the customer signals they're happy with the marriage; this is a relationship/handoff question, not a code question.
6. **CLI parity audit** (Section B) — separate spec, would benefit from its own pass against ADK's `adk` command surface.

The first two together are probably a half-day. The Dependencies decision is a longer conversation. Customer fork consolidation depends on the customer.
