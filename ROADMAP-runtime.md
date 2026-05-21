# Runtime Roadmap

The agent runtime that compiles to Model Serving or Apps: tools, identity passthrough,
caching, memory, interop. Goal: every primitive carries its own governance, every
expensive call is cached the right way, and the same agent talks to humans, to
itself across long runs, and to other agents across orgs.

Pairs with [`ROADMAP-dev-ui.md`](ROADMAP-dev-ui.md) (developer surface).

## Shipped

- **Governed tool factories** — `uc_function_tool`, `genie_tool`, `vector_search_tool`,
  `warehouse_tool`. Each declares itself as a Mosaic AI resource for scoped token
  minting at deploy time.
- **Identity passthrough (OBO)** — caller's OAuth token flows through every tool,
  every sub-agent call, every outbound Databricks API call. Framework-handled; no
  auth code at the tool level.
- **Workflow agents** — `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `RouterAgent`,
  `HandoffAgent`, `RemoteAgent`. Composable deterministic edges; `agent_tool` for
  LLM-driven delegation.
- **Dual compile target** — `apx deploy --target model-serving` produces a
  `ChatAgent`; `--target apps` produces a `ResponsesAgent` + Asset Bundle. Same
  agent code either way.
- **MLflow `apx.*` trace schema** — unified span shape across both runtimes, viewable
  in the dev UI trace browser.
- **Canary + hot-swap** — gradual rollout for both targets, with rollback.
- **MCP integration** — managed and unmanaged MCP servers as tools.
- **Memory** — in-memory, Lakebase, and delta-backed memory stores with
  consolidation.

## Next up

The expensive-call problem first, then interop, then async.

### 1. Two-tier Genie cache

Cache the *generated SQL*, not the result — so cache hits re-execute against fresh
data while skipping the Genie API call. Both layers ship together.

- **L1: per-replica LRU.** Exact-question match, O(1), bounded capacity, TTL.
- **L2: shared semantic cache.** Question embedding + cosine similarity in Lakebase
  pgvector. Cross-replica. Per-Genie-space partitioning to prevent cross-pollution.
- **Conversation context awareness.** Embed last N turns alongside the question;
  weighted score (`question_weight`, `context_weight`). Catches pronoun follow-ups
  ("what about *them*?").
- **Auto-tuned pgvector index.** `ivfflat_lists = max(100, sqrt(rows))`, `probes =
  max(10, sqrt(lists))`. Scales past 1M rows without manual knobs.

Lakebase is the natural home — no new infra, OBO works because the user already
owns the pgvector table grants.

### 2. Cache invalidation: feedback tool + circuit breaker

Stale cache is the dominant failure mode of #1. Two mechanisms ship with it:

- **Feedback tool.** Auto-generated `{tool_name}_feedback(rating, reason)` exposed
  to the LLM. Negative ratings invalidate the cache entry across all layers and
  forward the signal to Genie.
- **Circuit breaker.** `max_consecutive_cache_hits=N`: if the same cached SQL
  hash is returned N times in a row, auto-invalidate. Defeats LLM normalization
  loops where the model keeps asking the same canonical question.
- **Tool response enrichment.** `cache_hit`, `consecutive_cache_hits`,
  `auto_invalidated` surfaced back to the LLM and to MLflow spans so the dev UI
  can show it.

### 3. Two-stage vector retrieval with local reranking

- Retrieve `num_results: 50` candidates from Vector Search.
- Rerank with a local FlashRank cross-encoder (`ms-marco-MiniLM-L-12-v2` default,
  configurable). No external API call.
- Return top `top_n: 5`.
- Wire it into `vector_search_tool` via a `rerank: {model, top_n}` argument so
  existing retrievers opt in with one line.

### 4. A2A protocol endpoints on Apps deployments

Auto-mount Google [Agent2Agent](https://github.com/google/A2A) endpoints on every
`--target apps` deployment. Same agent, second public contract.

- `/.well-known/agent.json` discovery document generated from the agent definition
  (name, description, skills, input/output schemas).
- `POST /tasks/send`, `POST /tasks/sendSubscribe`, `GET /tasks/{id}` mapped onto
  the same handler as `/invocations`.
- OBO preserved across A2A calls — verify auth header forwarding works the same
  as in the ResponsesAgent path.

This is the interop wedge. Cross-vendor agent-to-agent calls are coming whether
we like them or not; shipping A2A from day one is cheap, and it differentiates
against frameworks that only speak their own runtime.

### 5. Long-running agent runs (kickoff / poll / cancel)

Multi-minute graph runs blow past Model Serving and Apps request timeouts today.
Add an async contract on top of the existing sync one:

- `POST /runs` → returns `{run_id, status: "queued"}`.
- `GET /runs/{run_id}` → status + partial output + trace_id.
- `POST /runs/{run_id}/cancel` → cooperative cancellation via callback.
- State persisted in Lakebase (reuse the memory backend). Same OBO context
  re-hydrated on each poll so resumption stays user-scoped.
- Surface in dev UI as a "long-run" panel that polls the same endpoints.

## Backlog

Things worth doing but not on the critical path.

- **Inline tool definitions.** YAML-or-Python inline tool blobs for prototyping
  without a separate module. Low-priority because the Python DSL already makes
  this fast.
- **External-agent-as-tool.** First-class `agent_endpoint_tool(url=...)` that
  wraps any A2A-speaking endpoint as a tool on the parent agent. Falls out of
  #4 cheaply once A2A is in.
- **Assert / Suggest / Refine middleware.** Pluggable pre/post-LLM hooks beyond
  the current callback system. Wait until customer pull is concrete.
- **Cache observability surface.** Hit-rate, latency-saved, and cost-avoided
  panels per tool in the dev UI. Probably falls out of #1+#2 trace data.
- **Cross-region cache federation.** Multi-workspace Genie caches that share an
  L2 store. Only matters past a certain scale.
- **Streaming reranking.** Re-rank as candidates arrive, not after all 50. Real
  but probably premature.

## Non-goals

What we are deliberately not building.

- **YAML config surface.** The Python DSL is the agent definition; YAML adds a
  second representation to keep in sync. Teams that want pure infra-as-code have
  good options elsewhere. We win on type-checked, IDE-assisted agent code.
- **A second visual builder direction.** `apx-builder` already exists; further
  visual tooling lives there, not in the runtime.
- **Generic CrewAI / Autogen / Agno adapters.** apx-agent's value is *Databricks
  governance + dual compile target*. Wrapping frameworks that already produce
  their own runtimes earns us a maintenance tax with no governance win. (See
  separate adapter ADR if this changes.)
- **Replacing MLflow eval.** The dev-UI eval tab is for fast inner-loop iteration.
  Production eval/regression stays in MLflow.

## Prior art and credits

The two-tier Genie cache, feedback-based invalidation, and FlashRank reranking
shapes here are informed by [natefleming/dao-ai](https://github.com/natefleming/dao-ai)'s
implementation. Convergent evolution on the broader runtime shape — declarative
agents, LangGraph engine, dual Databricks target — is its own signal that the
niche is real.
