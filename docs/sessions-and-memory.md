# Sessions + memory bank + example bank

## Sessions — multi-turn memory

By default, every `predict()` call is independent — the agent has no memory of prior turns. For conversational agents, pass a `SessionStore` to `compile_to_chat_agent` and include a `session_id` in `custom_inputs`. The framework loads the session before the LLM sees the new turn, prepends prior history, runs the agent, then persists the new messages.

```python
from apx_agent import (
    Agent, compile_to_chat_agent, DeltaSessionStore,
)
from databricks.sdk import WorkspaceClient

ws = WorkspaceClient()

session_store = DeltaSessionStore(
    table_path="main.agents.sessions",
    ws=ws,
    warehouse_id="wh-prod",   # explicit warehouse for predictable cost
)

agent = Agent(instructions="You help debug data pipelines.", tools=[...])
chat = compile_to_chat_agent(agent, model="databricks-claude-sonnet-4-6", session_store=session_store)

# Turn 1
chat.predict(
    messages=[ChatAgentMessage(role="user", content="why is finance.gold.revenue empty?")],
    custom_inputs={"session_id": "user:alice:thread-42"},
)

# Turn 2 — history from turn 1 is automatically prepended
chat.predict(
    messages=[ChatAgentMessage(role="user", content="what about yesterday?")],
    custom_inputs={"session_id": "user:alice:thread-42"},
)
```

| Store | When to use |
|-------|-------------|
| `InMemorySessionStore` | Tests, dev, single-process Apps |
| `DeltaSessionStore` | UC-governed Delta table. Analytics-style multi-step pipelines, durable across long-idle sessions. |
| `LakebaseSessionStore` | Lakebase (managed Postgres) via SQLAlchemy. Low-latency chat-style sessions; cheaper at high turn rates than Delta. |

Custom stores satisfy the `SessionStore` protocol (`get`/`put`/`delete`) — bring your own Redis, Memcached, etc.

`LakebaseSessionStore` takes a SQLAlchemy `Engine` and stays narrow on SQL — the caller wires up OAuth token rotation on the engine. From a git clone of this repo, install the lakebase extra with `cd apx-agent/python && pip install -e '.[lakebase]'`.

When `custom_inputs["session_id"]` is absent the framework silently runs single-turn — the same compiled agent works in both modes.

## Memory bank — long-lived recall across conversations

Sessions hold a single conversation. **MemoryBank** holds durable facts per principal (user, customer, agent) with semantic recall. Same backing stores as sessions (Lakebase pgvector, Delta + Vector Search), different access pattern: `principal_id` + `namespace` scoping, vector retrieval by query.

```python
from apx_agent import LakebaseMemoryStore, make_memory_tools, assemble_memory_context

# Caller wires the embedding source — no SDK import inside the package.
def embed(texts):
    return ws.serving_endpoints.query(
        name="databricks-bge-large-en", inputs={"input": list(texts)}
    ).predictions

store = LakebaseMemoryStore(
    engine=engine,
    embedding_fn=embed,
    embedding_dim=1024,
)

# Write
store.add(
    principal_id="user:alice",
    content="Prefers email summaries on Mondays, not Slack pings.",
    namespace="profile",
    tags=["preference", "notification"],
    importance=0.8,
)

# Read (semantic)
hits = store.recall(
    principal_id="user:alice",
    query="how should I notify alice?",
    namespace="profile",
    k=3,
)
```

Use it three ways:

1. **As tools the LLM calls** — `make_memory_tools(store, principal_id_resolver=...)` returns `recall` / `remember` / `forget` callables bound to the store.
2. **As prompt-assembly** — `assemble_memory_context(store, opts={"principal_id": ..., "query": ...})` returns a markdown block to prepend to `instructions`.
3. **As raw CRUD** — `store.add` / `recall` / `list` / `update` / `delete` for offline batch flows.

Stores: `InMemoryMemoryStore` (dev/tests), `LakebaseMemoryStore` (pgvector, low-latency chat-style), `DeltaMemoryStore` (Delta with optional `VectorSearchClient` delegation for managed indices, client-side cosine fallback, or recency-only fallback).

**Consolidation**: `consolidate_memories(store, principal_id, summarize_fn=...)` LLM-summarizes older/low-importance memories into a rollup row (with optional deletion of originals). Keeps the bank tractable as it grows.

## Example bank — few-shot retrieval

Same shape as MemoryBank, different scope key: `agent_id` + `intent`. Stores per-agent input/output exemplars used for in-context learning. `findSimilar` ranks by similarity of the *input* field (so "examples whose inputs look like this query" works).

```python
from apx_agent import LakebaseExampleStore, mine_examples

store = LakebaseExampleStore(engine=engine, embedding_fn=embed, embedding_dim=1024)

# Cold-start from real session history
result = mine_examples(
    session_store=session_store,
    example_store=store,
    agent_id="triage",
    score_fn=lambda turn: heuristic_score(turn),  # optional
    min_score=0.6,
)
print(f"Added {result.examples_added} examples from {result.sessions_scanned} sessions")
```

CLI parity for both:

```bash
apx memory recall   --principal-id user:alice --query "notification preferences" -k 3
apx memory remember --principal-id user:alice --content "..." --importance 0.8
apx examples find   --agent-id triage --query "why is my bill high?" -k 5
apx examples save   --agent-id triage --input "..." --output "..." --score 0.9
```

Store loaded via `--store-module MODULE:VAR` or `[tool.apx.agent].memory_store` / `example_store` in `pyproject.toml`.

A worked example lives in [`python/examples/memory_demo/`](../python/examples/memory_demo/) — seeded memories, recall/remember tools wired in, prompt assembly building the system prompt at every turn.
