# Sessions, memory, and example banks

> **Coming from ADK?** Sessions ≈ ADK `Session` + `SessionService`. MemoryStore ≈ ADK `MemoryService` (cross-session). ExampleBank has no direct ADK equivalent — it's few-shot retrieval from real session history.
>
> **Coming from OpenAI Agents SDK?** Sessions ≈ `session` strategy (persistent store) or `conversation_id` (server-managed). MemoryStore is the cross-session equivalent of `to_input_list()` with a vector index on top.

---

## Sessions — multi-turn conversation state

A **session** is a named conversation container. Give a turn a `session_id` string and the framework loads history before calling the LLM, appends the new exchange, and persists it. Without a session ID, every call starts fresh — same compiled agent, two different modes.

```
Session lifecycle
┌──────────────────────────────────────┐
│  predict(messages, session_id="s1")  │
│    ↓                                 │
│  Load history from store             │
│    ↓                                 │
│  Prepend history → call LLM          │
│    ↓                                 │
│  Append exchange → persist           │
└──────────────────────────────────────┘
```

### Session stores

Three stores cover the main workloads:

| Store | Best for | Latency |
|-------|----------|---------|
| `InMemoryConversationStore` | Tests, dev, single-process Apps | in-process |
| `DeltaConversationStore` | UC-governed Delta table; durable across long-idle sessions | ~100–500 ms/turn |
| `LakebaseConversationStore` | Chat UIs and high-frequency turns (Lakebase managed Postgres) | ~1–10 ms/turn |

Custom stores satisfy the `ConversationStore` protocol (the abstract `create_conversation` / `get_conversation` / `append` / `list_items` / `update_conversation` / `delete_conversation` / `list_conversations` / `search` methods) — bring your own Redis, Memcached, etc.

### Wiring a session store

Pass the store to `compile_to_chat_agent` and include `session_id` in `custom_inputs`:

```python
from apx_agent import (
    Agent, compile_to_chat_agent, DeltaConversationStore,
)
from databricks.sdk import WorkspaceClient

ws = WorkspaceClient()

conversation_store = DeltaConversationStore(
    table_prefix="main.agents.apx_conv",
    ws=ws,
    warehouse_id="wh-prod",   # explicit warehouse for predictable cost
)

agent = Agent(instructions="You help debug data pipelines.", tools=[...])
chat = compile_to_chat_agent(agent, model="databricks-claude-sonnet-4-6", conversation_store=conversation_store)

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

When `custom_inputs["session_id"]` is absent the framework silently runs single-turn — the same compiled agent works in both modes.

### Lakebase session store

`LakebaseConversationStore` takes a SQLAlchemy `Engine`. Always use the OBO token pattern — never a static password in the connection URL:

```python
from sqlalchemy import create_engine, event
from databricks.sdk import WorkspaceClient
from apx_agent import LakebaseConversationStore

ws = WorkspaceClient()
instance = ws.database.get_database_instance(name="my-lakebase-instance")

engine = create_engine(
    f"postgresql+psycopg://{ws.current_user.me().user_name}@{instance.read_write_dns}:5432/agentdb",
    pool_pre_ping=True,
    pool_recycle=1800,   # well under the ~1h Lakebase token TTL
)

@event.listens_for(engine, "do_connect")
def _refresh_token(_dialect, _record, _args, kwargs):
    cred = ws.database.generate_database_credential(
        instance_names=["my-lakebase-instance"],
        request_id="apx-sessions",
    )
    kwargs["password"] = cred.token

conversation_store = LakebaseConversationStore(engine=engine)
```

`do_connect` fires before every new connection is opened from the pool, so the password is always a fresh token. `pool_recycle=1800` ensures connections are dropped and re-opened before the token expires — never keep a connection alive across the full TTL.

See [`docs/lakebase-recipe.md`](lakebase-recipe.md) for provisioning, pgvector, pool tuning, and token rotation gotchas.

Install the lakebase extra: `pip install 'apx-agent[lakebase]'`.

---

## Memory bank — long-lived recall across conversations

Sessions hold a single conversation. **MemoryStore** holds durable facts per principal (user, customer, agent) with semantic recall — it persists across sessions and is searched by meaning, not by recency.

```
Memory access patterns
┌─────────────────────────────────────────────┐
│  store.add(principal_id, content, ...)      │  ← write a fact
│  store.recall(principal_id, query, k=3)     │  ← semantic retrieval
│  store.list(principal_id, namespace=...)    │  ← enumerate entries
│  store.update(memory_id, ...)               │  ← revise a fact
│  store.delete(memory_id)                    │  ← remove a fact
└─────────────────────────────────────────────┘
```

Same backing stores as sessions (Lakebase pgvector, Delta + Vector Search), different access pattern: `principal_id` + `namespace` scoping, vector retrieval by query.

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

# Write a fact
store.add(
    principal_id="user:alice",
    content="Prefers email summaries on Mondays, not Slack pings.",
    namespace="profile",
    tags=["preference", "notification"],
    importance=0.8,
)

# Semantic recall
hits = store.recall(
    principal_id="user:alice",
    query="how should I notify alice?",
    namespace="profile",
    k=3,
)
```

### Three ways to use MemoryStore

**1. As tools the LLM calls**

`make_memory_tools(store, principal_id_resolver=...)` returns `recall` / `remember` / `forget` callables bound to the store. Add them to your agent's `tools` list and the LLM decides when to read or write memory.

```python
from apx_agent import Agent, make_memory_tools

agent = Agent(
    instructions="...",
    tools=[*make_memory_tools(store, principal_id_resolver=lambda ctx: ctx.user_id)],
)
```

This is the closest equivalent to ADK's `PreloadMemory` (always-on retrieval) and `LoadMemory` (agent-initiated) built-in tools.

**2. As prompt-assembly**

`assemble_memory_context(store, opts={"principal_id": ..., "query": ...})` returns a markdown block to prepend to `instructions`. Use this when you want deterministic recall every turn without giving the LLM control over when to load memory.

```python
from apx_agent import assemble_memory_context

def build_instructions(user_id: str, query: str) -> str:
    memory_block = assemble_memory_context(store, opts={"principal_id": user_id, "query": query})
    return f"You are a helpful assistant.\n\n{memory_block}"
```

**3. As raw CRUD**

`store.add` / `recall` / `list` / `update` / `delete` for offline batch flows — seeding memories from external systems, cleaning up stale facts, etc.

### Memory stores

| Store | Best for |
|-------|----------|
| `InMemoryMemoryStore` | Tests, dev |
| `LakebaseMemoryStore` | pgvector, low-latency chat-style recall |
| `DeltaMemoryStore` | Delta with optional `VectorSearchClient` delegation, client-side cosine fallback, or recency-only fallback |

### Memory consolidation

`consolidate_memories(store, principal_id, summarize_fn=...)` LLM-summarizes older or low-importance memories into a rollup row (with optional deletion of originals). Keeps the bank tractable as it grows.

---

## Example bank — few-shot retrieval

Same shape as MemoryStore, different scope key: `agent_id` + `intent`. Stores per-agent input/output exemplars for in-context learning. `findSimilar` ranks by similarity of the *input* field — "examples whose inputs look like this query" — not by recency.

```python
from apx_agent import LakebaseExampleStore, make_example_tools

store = LakebaseExampleStore(engine=engine, embedding_fn=embed, embedding_dim=1024)

# Seed exemplars — one mapping each, or add_batch for many.
store.add_batch([
    {"agent_id": "triage", "input": "why is my bill so high?",
     "output": "Checked usage vs. plan — the overage was roaming data.", "score": 0.9},
    {"agent_id": "triage", "input": "I think I was double charged",
     "output": "Found the duplicate authorization and reversed it.", "score": 0.9},
])

# Wire recall as an agent tool (ranks by similarity of the input field).
tools = make_example_tools(store=store, agent_id_resolver=lambda: "triage")
```

You can also seed from the CLI (`apx-agent examples save ...`, below) or let the
agent capture its own exemplars at runtime via the `save_example` tool.

---

## CLI

```bash
apx-agent memory recall   --principal-id user:alice --query "notification preferences" -k 3
apx-agent memory remember --principal-id user:alice --content "..." --importance 0.8
apx-agent examples find   --agent-id triage --query "why is my bill high?" -k 5
apx-agent examples save   --agent-id triage --input "..." --output "..." --score 0.9
```

Store loaded via `--store-module MODULE:VAR` or `[tool.apx.agent].memory_store` / `example_store` in `pyproject.toml`.

A worked example lives in [`python/examples/memory_demo/`](../python/examples/memory_demo/) — seeded memories, recall/remember tools wired in, prompt assembly building the system prompt at every turn.

---

## Decision guide

| Need | Use |
|------|-----|
| Within-conversation history | Conversation store — pass `session_id` |
| Cross-session facts (user preferences, past decisions) | MemoryStore — `make_memory_tools` or `assemble_memory_context` |
| Few-shot examples for a specific agent | `ExampleStore` — seed with `store.add`/`add_batch` (or `examples save`), recall via `make_example_tools` |
| Fast dev/test, single process | `InMemoryConversationStore` / `InMemoryMemoryStore` |
| Durable, Unity Catalog governed | `DeltaConversationStore` / `DeltaMemoryStore` |
| Low-latency chat (high turns/sec) | `LakebaseConversationStore` / `LakebaseMemoryStore` |
