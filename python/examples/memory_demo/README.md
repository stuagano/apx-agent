# memory_demo

A self-contained worked example showing how to wire **MemoryBank** + **ExampleStore**
into an `apx-agent` end-to-end.

## What's in here

- `app.py` — full demo: stores, seeded data, `make_memory_tools` for the
  `recall` / `remember` / `forget` callables, an assembled system prompt
  built via `assemble_context`, and a reproducible round-trip path that
  runs without an LLM endpoint.

## What was added vs. a plain agent

1. An `InMemoryMemoryStore` and `InMemoryExampleStore` instantiated at module
   load, pre-seeded with five memories and three few-shot examples for a fake
   principal (`alice`).
2. The agent's tool set is built by `make_memory_tools(store, default_principal_id=...)`
   — `recall` / `remember` / `forget` are bound to the store automatically.
3. The system prompt is built by `assemble_context(memory=..., examples=...)` —
   the helper pulls relevant memories + few-shot examples at compile time and
   formats them as markdown blocks before the static instructions.
4. A `_demo()` entry point prints (a) the assembled system prompt, (b) a
   mid-turn `recall` tool call, and (c) an after-response `remember` tool
   call that persists a new fact.

## Run

```bash
cd python
uv run python -m examples.memory_demo.app
```

The demo runs entirely in-process — no Databricks workspace, model endpoint,
or Lakebase instance required. Swap `InMemoryMemoryStore` →
`LakebaseMemoryStore` (and same for examples) for shared persistence across
replicas, or `DeltaMemoryStore` / `DeltaExampleStore` for Delta + Vector
Search delegation.
