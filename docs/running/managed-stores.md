# Moving from Lakebase stores to managed stores

apx's own Lakebase stores are deprecated
([ADR 0001](../adr/0001-build-on-agent-bricks.md)). Managed stores are now the
only durable backend for new agents. `inmemory` is still there for local dev
and tests. Memory still defaults to `off`, and Lakebase is never required.

Agents that declare `type="lakebase"` for `memory=` or `session=` keep working
for now. The session declaration also covers the durable checkpointer. Each of
these stores logs a deprecation warning once per process, and
`apx-agent doctor` shows a `Lakebase stores` WARN row.

## Switch the declaration

```python
# Before
agent = Agent(
    instructions="...",
    session={"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "my_agent"},
    memory={"type": "lakebase", "host": "${LAKEBASE_HOST}", "database": "my_agent",
            "embedding_model": "databricks-bge-large-en", "embedding_dim": 1024},
)

# After
agent = Agent(
    instructions="...",
    session={"type": "managed", "store_name": "my-agent-sessions"},
    memory={"type": "managed", "store_name": "agent-memory"},
)
```

Then remove the `lakebase` extra from the `apx-agent` dependency, and the
`LAKEBASE_HOST` env var if nothing else uses it. Run `apx-agent doctor` to
confirm the WARN row is gone.

## What does not move

- **History.** Switching to a managed store doesn't copy existing sessions or
  memories, which matches upstream behaviour. Old rows stay in your Lakebase
  database until you drop them.
- **Retention.** Managed memory rejects `retention`, because the platform
  doesn't state a retention period.
- **Extraction and consolidation.** apx's own memory extraction, strategies
  and consolidation were removed. An Agent Bricks memory pipeline distills a
  managed session store into a managed memory store instead; see
  [Memory pipeline](#memory-pipeline).

## Memory pipeline

1. Move memory and session to managed stores (above).
2. Write what you had in `memory.strategy_prompts` or
   `memory.extraction.instructions` into one instructions file. The pipeline
   takes free text, not typed `facts` / `preferences` / `summary` records.
3. Remove `extraction`, `strategies`, `strategy_prompts` and
   `extract_every_n_turns` from `memory=`.
4. Create the pipeline, then run `apx-agent doctor` to confirm the
   `Memory pipeline` row is OK:

   ```bash
   agentbricks memory pipeline create \
     --session-store <sessions> --memory-store <memory> \
     --instructions @memory-instructions.md --trigger scheduled
   ```

A scheduled pipeline runs about every 24 hours, not every N turns. Entries are
organized by topic under each actor, not in one namespace per strategy.

For per-store behaviour, see [sessions-and-memory.md](sessions-and-memory.md).
