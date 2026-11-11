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
- **Extraction and consolidation.** apx's own memory extraction and
  consolidation are deprecated too. Use `agentbricks memory pipeline` instead.

For per-store behaviour, see [sessions-and-memory.md](sessions-and-memory.md).
