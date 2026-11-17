# memory_demo

A self-contained worked example showing how to wire **MemoryStore** + **ExampleStore**
into an `apx-agent` end-to-end.

`memory_demo` ships in two run modes:

| Mode | Entry point | Infra required | Use case |
|---|---|---|---|
| **Local-only** | `app.py` | none — pure Python | Confirm the memory wiring round-trips in a single process. No LLM endpoint, no workspace. |
| **Databricks Apps** | `agent_server/` + `databricks.yml` | Databricks workspace + serving endpoint | Real deploy. `agents deploy --target apps --profile <profile>` creates or updates the App through the Apps API and rolls it out. No container build queue. |

Both modes share the same `Agent`, the same memory + example stores, the
same `make_memory_tools` callables, and the same `assemble_context`
system-prompt assembly. The only difference is whether you import the
module locally or behind a FastAPI route inside an App.

## What's in here

- `app.py` — the canonical in-process demo. Seeds the stores, builds the
  system prompt, runs the `recall` / `remember` tools synchronously, and
  prints the round-trip to stdout. No LLM, no infra.
- `agent.py` — the same `Agent` + memory/example-store wiring, shared with the
  local demo.
- `agent_server/start_server.py` — compiles `agent` with
  `compile_agent(..., target="durable_agent_server")`, which serves it on
  `DurableAgentServer`. Databricks Apps runs this file via uvicorn.
- `databricks.yml` — Asset Bundle config. Declares the App + the
  serving-endpoint resource the App connects to.
- `pyproject.toml` — apps-shape dependencies (apx-agent with the `agentbricks` extra, mlflow, fastapi, uvicorn).
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
apx-agent agents deploy --target apps --profile <profile>
```

The App runs as a `DurableAgentServer`. `[tool.apx.deploy]` in `pyproject.toml`
selects the Apps API backend (`backend = "apps_api"`, `entrypoint = "agent_server"`),
which keeps the `memory-demo` app name instead of requiring an `agent-bricks-`
prefix. The Apps API deploy needs an explicit `--profile`, and `--no-run`,
`--var` and `--secret-env` are not supported. `apx-agent agents deploy --target apps`:

1. Stages `.build/` (source + wheel) and its `uv.lock`
2. Auto-resolves an MLflow experiment id (creates `/Users/<you>/memory-demo-dev`
   if missing, reuses if present)
3. Creates or reuses the App, applies grants, generates `app.yaml`
   (`python -m agent_server`), then uploads and rolls out
4. Verifies the rollout (App state and the `/readyz` gate)

The App reads `X-Forwarded-Access-Token` from each request, so tool calls run
as the calling user, not the App's service principal.

## What was added vs. a plain agent

1. An `InMemoryMemoryStore` and `InMemoryExampleStore` instantiated at module
   load, pre-seeded with five memories and three few-shot examples for a fake
   principal (`alice`).
2. The agent's tool set is built by
   `make_memory_tools(..., default_principal_id=..., _use_dep_principal=True)`
   — `recall` / `remember` / `forget` are bound to the store and resolve the
   per-request OBO principal (`Dependencies.Principal`) when Apps headers are
   present. `default_principal_id="alice"` is the local/smoke fallback only.

   **DEPLOY BLOCKER:** do not run multi-user production traffic against the
   alice fallback — callers without `X-Forwarded-User` would share one memory
   scope. Require Apps headers, drop the default, or supply a
   `principal_id_resolver` that never returns a shared demo id.
3. The system prompt is built by `assemble_context(memory=..., examples=...)` —
   the helper pulls relevant memories + few-shot examples at compile time and
   formats them as markdown blocks before the static instructions.
4. A `_demo()` entry point in `app.py` prints (a) the assembled system prompt,
   (b) a mid-turn `recall` tool call, and (c) an after-response `remember`
   tool call that persists a new fact.
5. The Apps target compiles the same `agent` onto `DurableAgentServer` with
   `compile_agent(agent, target="durable_agent_server", ...)`.

## Swapping in durable stores

The in-memory stores keep the demo runnable without infra. Production
replaces them:

| Want | Swap to | See |
|---|---|---|
| Shared state across App replicas | `LakebaseMemoryStore`, `LakebaseExampleStore` | [`docs/running/lakebase-recipe.md`](../../../docs/running/lakebase-recipe.md) |
| Delta + Vector Search backing | `DeltaMemoryStore`, `DeltaExampleStore` | `apx_agent._memory_delta` / `_example_delta` source |

When you move to Lakebase, also add a `database` resource to `databricks.yml`
under the `memory-demo` app so the App SP gets DB credentials.
