# Lakebase recipe — provisioning + pgvector + apx-agent wiring

End-to-end recipe for running apx-agent's session, memory, and few-shot example stores on Databricks Lakebase (managed Postgres) with pgvector. Every snippet here is copy-paste-adapt: real CLI invocations, real DDL, real wiring code that matches the actual store APIs in `python/src/apx_agent/_conversation_lakebase.py`, `_memory_lakebase.py`, and `_example_lakebase.py`.

## 1. Why Lakebase for agent state

apx-agent has three durable stores, each with a Lakebase variant:

| Store | What it holds | Access pattern |
|---|---|---|
| `LakebaseConversationStore` | Per-conversation history + state | Point lookup by `session_id`, frequent UPSERT |
| `LakebaseMemoryStore` | Per-principal long-lived facts | Vector recall + tag/namespace filter |
| `LakebaseExampleStore` | Per-agent few-shot examples | Vector retrieval over `(input, output)` pairs |

Why Postgres for these:

- **Point-lookup latency.** A `SELECT ... WHERE session_id = ?` against Postgres returns in single-digit milliseconds — well suited to an interactive chat surface that hits the store on every turn.
- **pgvector in SQL.** Cosine ranking via `embedding <=> :q::vector` runs server-side. No client-side scoring loop and no extra Vector Search index to keep in sync with the source-of-truth table.
- **Per-user OAuth.** Lakebase mints short-lived database credentials per request via `databricks database generate-database-credential`, so the calling user's identity threads through to Postgres-level row-level security if you wire it.

For memory specifically, `ManagedMemoryStore` (UC managed memory, GA) is a no-extra-infra alternative when the semantic-recall workload doesn't justify an always-on Lakebase instance — see [sessions-and-memory.md](sessions-and-memory.md). It covers long-term memory only; there's no managed equivalent for session/conversation history or few-shot examples.

## 2. One-time provisioning

### Create the instance

```bash
# Pick a name and capacity SKU. CU_1 / CU_2 / CU_4 — see `--help` for the current list.
databricks database create-database-instance my-agent-db \
  --capacity CU_1 \
  --node-count 1 \
  --retention-window-in-days 7 \
  --profile prod
```

The CLI waits for the instance to reach `AVAILABLE`. Pass `--no-wait` if you want to background it; `databricks database get-database-instance my-agent-db` polls status.

### Workspace permissions

Anyone whose identity mints database credentials needs:

- `CAN_USE` on the Database Instance (Lakebase ACL).
- A Lakebase Postgres role wired to their workspace identity. Lakebase auto-provisions a role per Databricks user the first time `generate-database-credential` is called for them; for service principals you grant it explicitly via SQL once.
- Permission to call `databricks.workspace_client.WorkspaceClient.database.generate_database_credential` (workspace-level token, no extra scope).

For a service principal doing the agent's writes, mint the role once:

```sql
-- One-time inside Postgres, executed as a Lakebase admin role.
CREATE ROLE "agent-sp" WITH LOGIN;
GRANT ALL PRIVILEGES ON DATABASE agentdb TO "agent-sp";
```

### pgvector

Lakebase ships with the `vector` extension preinstalled — you do not need to install it. You do need to enable it once per database; the framework's `LakebaseMemoryStore` and `LakebaseExampleStore` run `CREATE EXTENSION IF NOT EXISTS vector` automatically the first time DDL runs (set `ensure_extension=False` if your role lacks `CREATE EXTENSION`).

Verify manually:

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

## 3. SQLAlchemy engine setup with OAuth rotation

Lakebase tokens are short-lived (about 1 hour). The recommended pattern is a SQLAlchemy `do_connect` event listener that mints a fresh token per connection:

```python
from sqlalchemy import create_engine, event
from databricks.sdk import WorkspaceClient

ws = WorkspaceClient()

# Build the connection URL — host comes from the instance, username is the
# Databricks user/SP name, password is filled by the event listener below.
instance = ws.database.get_database_instance(name="my-agent-db")
host = instance.read_write_dns
user = ws.current_user.me().user_name  # or your service principal name

url = f"postgresql+psycopg://{user}@{host}:5432/agentdb"

engine = create_engine(
    url,
    pool_pre_ping=True,         # detect stale connections before use
    pool_recycle=1800,          # recycle every 30 min — under the 1-hour token TTL
    pool_size=5,
    max_overflow=10,
)

@event.listens_for(engine, "do_connect")
def add_oauth_token(_dialect, _conn_record, _cargs, kwargs):
    cred = ws.database.generate_database_credential(
        instance_names=["my-agent-db"],
        request_id="apx-agent",  # any short identifier — shows up in audit logs
    )
    kwargs["password"] = cred.token
```

Pool tuning notes:

- `pool_recycle=1800` is the load-bearing knob. The default Lakebase token TTL is one hour. Set the pool to drop and re-mint well under that boundary.
- `pool_pre_ping=True` adds a cheap `SELECT 1` on checkout; the round-trip cost is in the single-digit ms range and avoids the "connection died while idle" failure mode at the cost of one extra packet per checkout.
- `pool_size` + `max_overflow` should reflect the agent's parallelism. For a Mosaic AI Model Serving endpoint with a 4-replica scale, `pool_size=5, max_overflow=10` per replica is a safe baseline.

## 4. Wire `LakebaseConversationStore`

```python
from apx_agent import LakebaseConversationStore, compile_to_chat_agent

conversation_store = LakebaseConversationStore(
    engine=engine,
    conversations_table="apx_conversations",   # quoted as SQL identifiers; can be schema-qualified
    items_table="apx_conversation_items",
    auto_create=True,                          # CREATE TABLE IF NOT EXISTS on first use
)

chat = compile_to_chat_agent(my_agent, model="databricks-claude-sonnet-4-6",
                             conversation_store=conversation_store)
```

At deploy time, `predict(messages, custom_inputs={"session_id": "user:alice:thread-7"})` carries history automatically. Conversation rows are keyed by `conversation_id`; per-turn items are stored in the companion items table.

## 5. Wire `LakebaseMemoryStore`

Memory needs an embedding function. Wire it to a Foundation Model API endpoint:

```python
from apx_agent import LakebaseMemoryStore

def embed(texts):
    return ws.serving_endpoints.query(
        name="databricks-gte-large-en",   # FMAPI endpoint, 1024-dim
        inputs={"input": list(texts)},
    ).predictions

memory_store = LakebaseMemoryStore(
    engine=engine,
    embedding_fn=embed,
    embedding_dim=1024,              # must match the function's actual output
    table_name="apx_memories",
    auto_create=True,
    ensure_extension=True,           # set False if your role lacks CREATE EXTENSION
)
```

`databricks-gte-large-en` is the general-purpose 1024-dim Foundation Model API endpoint Databricks ships in most workspaces — see the entity-resolution-agent example for prior art. If you swap to a different embedder, set `embedding_dim` to whatever it actually returns.

The store auto-creates the table + indexes on first call:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS apx_memories (
  id           TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  namespace    TEXT NOT NULL DEFAULT 'default',
  content      TEXT NOT NULL,
  tags         TEXT[] NOT NULL DEFAULT '{}',
  importance   REAL NOT NULL DEFAULT 0.5,
  embedding    vector(1024),
  metadata     JSONB NOT NULL DEFAULT '{}',
  created_at   DOUBLE PRECISION,
  updated_at   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS apx_memories_principal_idx ON apx_memories (principal_id);
CREATE INDEX IF NOT EXISTS apx_memories_embedding_idx ON apx_memories
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

## 6. Wire `LakebaseExampleStore`

Same shape as memory; key difference is the row is keyed by `agent_id` not `principal_id`:

```python
from apx_agent import LakebaseExampleStore

example_store = LakebaseExampleStore(
    engine=engine,
    embedding_fn=embed,              # same FMAPI callable
    embedding_dim=1024,
    table_name="apx_examples",
    auto_create=True,
)
```

The table is `(id, agent_id, intent, input, output, score, tags, embedding, metadata, created_at, updated_at)` with `agent_id` indexed and `embedding` on an ivfflat index. The `input` field is what gets embedded — `find_similar` ranks `(input ≈ query)`.

## 7. Operational notes

### Indexes the framework creates

`LakebaseMemoryStore` and `LakebaseExampleStore` create two indexes per table on first DDL:

- A B-tree index on the principal/agent key — for filtered listing and metadata lookups.
- An ivfflat index on the `embedding` column with `vector_cosine_ops` and `lists = 100` — for similarity search.

### Tuning the ivfflat `lists` parameter

The default `lists = 100` is right for tables with roughly 1k–100k rows. The rule of thumb in the pgvector docs is `lists = rows / 1000` for tables up to 1M rows, and `lists = sqrt(rows)` above that. Re-tune by dropping and recreating the index:

```sql
DROP INDEX apx_memories_embedding_idx;
CREATE INDEX apx_memories_embedding_idx ON apx_memories
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1000);
```

At query time set `SET ivfflat.probes = <N>` per session to trade recall for latency; the default `probes = 1` gives the lowest latency and lowest recall, `probes = sqrt(lists)` is a balanced choice.

For workloads above 1M memories per principal, switch to `hnsw` indexes — they cost more to build but query faster and adapt better to growing tables. The framework's auto-DDL doesn't switch automatically; drop the ivfflat index and recreate as hnsw.

### Backup / restore

Lakebase honors standard Postgres backup tooling. `databricks database get-database-instance my-agent-db -o json` returns recent backup metadata. Restore is via `databricks database create-database-instance --json '{...}'` with a `parent_instance_ref` block — see `databricks database create-database-instance --help` for the current schema.

### Token rotation gotchas

- **Long-lived connections outlive their token.** The `pool_recycle=1800` knob handles this for healthy pools, but a connection sitting idle in the pool past 1 hour will fail mid-query. `pool_pre_ping` catches most of these.
- **`do_connect` mints a token on every new connection.** That's a workspace API call per checkout from an exhausted pool. Keep `pool_size` high enough that steady-state traffic reuses warm connections.
- **`generate_database_credential` is rate-limited.** Workspace limit is roughly 60 calls/minute per principal at the time of writing. If you see HTTP 429s, raise `pool_recycle` or cache the credential for a few minutes (still inside its TTL) at the listener level.

## 8. Migration: `InMemoryMemoryStore` → `LakebaseMemoryStore`

For agents that started life with the in-process store (typical during local development — see [`python/examples/customer_triage/`](../../python/examples/customer_triage/)), porting state forward is a one-shot script using `add_batch`:

```python
"""Migrate InMemoryMemoryStore contents into a LakebaseMemoryStore.

Idempotent — `add_batch` upserts by id, so re-running is safe.
"""
from apx_agent import LakebaseMemoryStore, InMemoryMemoryStore
from apx_agent._memory import MemoryFilter

# 1. The in-memory source — typically imported from the agent module that
#    seeded it during dev (e.g. `from examples.customer_triage.agent import
#    account_memory_store`).
src: InMemoryMemoryStore = account_memory_store  # noqa: F821

# 2. The destination, wired exactly as production runs it.
dst = LakebaseMemoryStore(
    engine=engine, embedding_fn=embed, embedding_dim=1024,
)

# 3. Walk every principal in the source. InMemoryMemoryStore doesn't expose
#    a "list principals" method by design (production stores would scan a
#    table); for migration we enumerate based on what the application knows.
PRINCIPALS = ["user:alice", "user:bob"]   # supplied by the caller

batch = []
for pid in PRINCIPALS:
    rows = src.list(MemoryFilter(principal_id=pid, limit=10_000))
    for m in rows:
        batch.append({
            "id": m.id,                  # preserve ids so re-runs upsert
            "principal_id": m.principal_id,
            "namespace": m.namespace,
            "content": m.content,
            "tags": list(m.tags),
            "importance": m.importance,
            "metadata": dict(m.metadata),
        })

if batch:
    dst.add_batch(batch)  # one round-trip for the whole batch's embeddings
print(f"migrated {len(batch)} memories")
```

A few notes:

- `add_batch` calls `embedding_fn` once for the whole batch, then UPSERTs row-by-row inside a single transaction. For very large source stores (>10k rows), chunk the input list before calling — embedding endpoints have request-size limits.
- Preserving `id` is what makes the script idempotent. If the in-memory source minted ids via `new_memory_id()`, those are stable UUID-flavored strings; if not, pass through whatever id field the caller set on add.
- `created_at` / `updated_at` are *not* preserved by the batch path — they're stamped fresh at insert time. For audit-faithful migration, fall back to the row-at-a-time path with `INSERT ... ON CONFLICT (id) DO NOTHING` and patch the timestamps directly.

The same shape works for `LakebaseExampleStore` — swap principal for `agent_id` and use `ExampleFilter`.

## Cross-references

- Python store source: [`python/src/apx_agent/_conversation_lakebase.py`](../../python/src/apx_agent/_conversation_lakebase.py), [`_memory_lakebase.py`](../../python/src/apx_agent/_memory_lakebase.py), [`_example_lakebase.py`](../../python/src/apx_agent/_example_lakebase.py)
- TypeScript equivalents: [`typescript/src/session-lakebase.ts`](../../typescript/src/session-lakebase.ts), [`memory-lakebase.ts`](../../typescript/src/memory-lakebase.ts), [`example-lakebase.ts`](../../typescript/src/example-lakebase.ts)
- Worked example with `InMemoryMemoryStore` ready to swap: [`python/examples/customer_triage/`](../../python/examples/customer_triage/)
- Full MemoryStore narrative: [root README](../../README.md)
