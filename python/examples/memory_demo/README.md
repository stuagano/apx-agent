# memory_demo

A self-contained worked example showing how to wire **memory** and **few-shot
examples** into an `apx-agent` end-to-end.

## What's in here

- `app.py` — full demo: stores, seeded data, inline `recall`/`remember` tools,
  an assembled system prompt with a recall block + few-shot examples, and a
  reproducible round-trip path that runs without an LLM endpoint.

## What was added vs. a plain agent

1. An `InMemoryMemoryStore` and `InMemoryExampleStore` instantiated at module
   load, pre-seeded with five memories and three few-shot examples for a fake
   principal (`alice`).
2. Two tools — `recall(query)` and `remember(content)` — defined inline against
   the store. (When the framework ships `make_memory_tools`, these become a
   one-liner; the agent shape is unchanged.)
3. An `_assemble_context(query_hint)` helper that pulls relevant memories +
   few-shot examples out of the stores at compile time and prepends them to
   the static instructions as a recall block. This is the **assemble_context**
   pattern — in a session-aware deployment it runs per-turn from a callback,
   not once at module load.
4. A `_demo()` entry point that prints (a) the assembled system prompt,
   (b) a mid-turn `recall` tool call, and (c) an after-response `remember`
   tool call that persists a new fact.

## Run

```bash
cd python
uv run python -m examples.memory_demo.app
```

The demo runs entirely in-process — no Databricks workspace, model endpoint,
or Lakebase instance required. Swap `InMemoryMemoryStore` →
`LakebaseMemoryStore` (and same for examples) for shared persistence across
replicas.
