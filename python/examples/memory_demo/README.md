# memory_demo

A self-contained worked example showing how to wire **MemoryBank** + **ExampleStore**
into an `apx-agent` end-to-end.

`memory_demo` ships in two run modes:

| Mode | Entry point | Infra required | Use case |
|---|---|---|---|
| **Local-only** | `app.py` | none — pure Python | Confirm the memory wiring round-trips in a single process. No LLM endpoint, no workspace. |
| **Databricks Apps** | `agent_server/` + `databricks.yml` | Databricks workspace + serving endpoint | Real deploy. Bundle pushes the agent to a Databricks App; `bundle run` restarts it. No container build queue. |

Both modes share the same `Agent`, the same memory + example stores, the
same `make_memory_tools` callables, and the same `assemble_context`
system-prompt assembly. The only difference is whether you import the
module locally or behind a FastAPI route inside an App.

`memory_demo` previously deployed via Model Serving (`databricks.agents.deploy`).
We moved the deploy story to Apps because Model Serving container builds queue —
iteration suffers. The full tradeoff write-up is in
[`docs/deploy/apps-vs-model-serving.md`](../../../docs/deploy/apps-vs-model-serving.md).

## What's in here

- `app.py` — the canonical in-process demo. Seeds the stores, builds the
  system prompt, runs the `recall` / `remember` tools synchronously, and
  prints the round-trip to stdout. No LLM, no infra.
- `agent_server/agent.py` — the same wiring, plus `compile_to_responses_agent`
  and `@invoke()`/`@stream()` registrations for the Databricks Apps target.
- `agent_server/start_server.py` — FastAPI entry point. Databricks Apps
  runs this file via uvicorn.
- `databricks.yml` — Asset Bundle config. Declares the App + the
  serving-endpoint resource the App connects to.
- `pyproject.toml` — apps-shape dependencies (apx-agent, mlflow, fastapi, uvicorn).
- `scripts/quickstart.py` — one-shot setup. Creates an MLflow experiment and
  writes `.env`. Run via `uv run quickstart`.
- `.env.example` — environment template.

## Run locally (no infra)

```bash
cd python
uv run python -m examples.memory_demo.app
```

The demo runs entirely in-process — no Databricks workspace, model endpoint,
or Lakebase instance required. Useful for confirming the memory store +
context assembly path before you point real traffic at the agent.

Output (truncated):

```
========================================================================
memory_demo — apx-agent memory + examples worked example
========================================================================
PRINCIPAL  : alice
AGENT      : travel_concierge
DEMO QUERY : what seat do I usually pick?

[assembled system prompt with seeded memories + few-shot examples]
[mid-turn recall tool call output]
[after-response remember tool call output]
done.
final stored memory count for alice: 6
```

## Deploy to Databricks Apps

```bash
cd python/examples/memory_demo
apx-agent deploy --target apps
```

One command. `apx-agent deploy --target apps` does it all:

1. Builds the apx-agent wheel from the parent source tree
2. Stages `.build/` (source + wheel), rewrites the staged `pyproject.toml`
   to use the wheel path
3. Regenerates `.build/uv.lock` against the rewritten pyproject
4. Auto-resolves an MLflow experiment id (creates `/Users/<you>/memory-demo-dev`
   if missing, reuses if present)
5. `databricks bundle validate + deploy + run`
6. Polls `databricks apps get memory-demo` until `app_status=RUNNING`
   and `compute_status=ACTIVE`

The App reads `X-Forwarded-Access-Token` from each request and threads it
into the compiled `ResponsesAgent` so tool calls run as the calling user,
not the App's service principal.

## What was added vs. a plain agent

1. An `InMemoryMemoryStore` and `InMemoryExampleStore` instantiated at module
   load, pre-seeded with five memories and three few-shot examples for a fake
   principal (`alice`).
2. The agent's tool set is built by `make_memory_tools(store, default_principal_id=...)`
   — `recall` / `remember` / `forget` are bound to the store automatically.
3. The system prompt is built by `assemble_context(memory=..., examples=...)` —
   the helper pulls relevant memories + few-shot examples at compile time and
   formats them as markdown blocks before the static instructions.
4. A `_demo()` entry point in `app.py` prints (a) the assembled system prompt,
   (b) a mid-turn `recall` tool call, and (c) an after-response `remember`
   tool call that persists a new fact.
5. The Apps target adds `compile_to_responses_agent(agent, model=...)` plus
   `@invoke()` / `@stream()` functions registered with
   `mlflow.genai.agent_server`.

## Swapping in durable stores

The in-memory stores keep the demo runnable without infra. Production
replaces them:

| Want | Swap to | See |
|---|---|---|
| Shared state across App replicas | `LakebaseMemoryStore`, `LakebaseExampleStore` | [`docs/running/lakebase-recipe.md`](../../../docs/running/lakebase-recipe.md) |
| Delta + Vector Search backing | `DeltaMemoryStore`, `DeltaExampleStore` | `apx_agent._memory_delta` / `_example_delta` source |

When you move to Lakebase, also add a `database` resource to `databricks.yml`
under the `memory-demo` app so the App SP gets DB credentials.
