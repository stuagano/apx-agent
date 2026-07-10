# Apps vs Model Serving — picking a deploy target for an apx-agent

apx-agent compiles to two production targets on Databricks: **Model Serving**
(the original `databricks.agents.deploy` path) and **Databricks Apps** (the
newer bundle-based path). They share a workspace, a Unity Catalog, and an
OBO model — but the deployment pipeline, runtime contract, and operational
controls differ.

This doc covers (1) when each target is the right pick, (2) why both exist,
(3) how apx-agent hides most of the difference, (4) the code-level changes
the framework absorbs on your behalf, and (5) how to migrate between them.

Sibling docs:

- [`memory_demo` README](../../python/examples/memory_demo/README.md) — worked
  example that runs in both modes.
- [`lakebase-recipe.md`](../running/lakebase-recipe.md) — durable agent state on
  Postgres. Both targets can use it.
- [`troubleshooting.md`](./troubleshooting.md) —
  per-target failure modes and how to read them.

## 1. When to use which

The short version: **default to Apps** for new agents unless you need a
Model-Serving-only feature. Model Serving still has the better story for
canary rollouts, traffic splits, the Review App, and Mosaic AI Supervisor
integration — but you pay for it with the container build queue, which is
the single biggest source of iteration pain in the SDK today.

| Concern | Model Serving | Databricks Apps |
|---|---|---|
| **Deploy speed (first deploy)** | 10–30 min (container build + image promote) | 1–3 min (code sync + worker restart) |
| **Deploy speed (iteration)** | Each iter rebuilds the container; queue depth wins | Edit code → `bundle deploy` → worker restart; seconds |
| **Build queue contention** | Real and frequent on busy workspaces | None — no shared build pool |
| **Runtime contract** | `mlflow.pyfunc.ChatAgent` | `mlflow.types.responses.ResponsesAgent` (via `@invoke`/`@stream`) |
| **Streaming envelope** | `ChatAgentChunk` deltas | `ResponsesAgentStreamEvent` chunks (SSE) |
| **Identity passthrough (OBO)** | `custom_inputs["user_token"]` | `X-Forwarded-Access-Token` header (Apps injects automatically) |
| **Async / WebSocket support** | No — request/response only | Yes — full FastAPI surface, SSE, WebSocket if you want it |
| **Co-located UI** | No (separate app or AI Playground) | Yes — front the same App with a SPA, same auth context |
| **Traffic split / canary** | Native (Model Serving served entities + traffic config) | Not built in — handle at app/route level or via two Apps + a router |
| **Canary deploy CLI** | `apx-agent canary deploy` (default) — adds a served entity at N% traffic via `databricks.agents.deploy` | `apx-agent canary deploy --target apps --canary-version X` — writes a `canary-X` DAB target and deploys a sibling App. Soak-environment semantics; no platform traffic split. See [apps-canary-hotswap-design.md](../engine-scope/apps-canary-hotswap-design.md) |
| **Hot-swap LLM endpoint** | `apx-agent agents hot-swap` (default) — rewrites `APX_AGENT_MODEL_OVERRIDE` env_var on the served entity | `apx-agent agents hot-swap --target apps --llm-endpoint NEW` — re-deploys with `--var llm_endpoint_name=NEW`. App restarts off the new env. See [apps-canary-hotswap-design.md](../engine-scope/apps-canary-hotswap-design.md) |
| **Review App integration** | Native — `agent_evaluation` Review App reads from a registered model | Indirect — log traces to MLflow, evaluate offline |
| **Mosaic AI Supervisor publish** | Native via `apx-agent supervisor add --supervisor <id>` | Not supported as of 2026-05 — Supervisor consumes Model-Serving endpoints |
| **Autoscale model** | Per-endpoint, scale-to-zero with cold-start cost — set from the CLI via `agents deploy --scale-to-zero` / `--workload-size` | Per-app, manual `min/max` workers; scale-to-zero supported via auto-suspend |
| **Async background work** | No (request-bound) | Yes — same process can run background tasks, schedulers, queues |
| **Custom routes / non-agent endpoints** | No — single predict path | Yes — health, admin, debug, batch endpoints all in the same App |
| **MLflow tracing wiring** | Automatic — Model Serving enables tracing on every request | Automatic via `AgentServer(agent_type="ResponsesAgent")` — same OTLP exporter |
| **Version ledger / discovery** | Native — each `databricks.agents.deploy` mints a UC registered-model version, tagged `apx.agent.*` | Via the UC-registry shim — `apx-agent agents deploy --target apps` registers a UC version *manifest* (tagged `apx.serving=apps`, not serving-promoted) so Apps agents get versions + show up in `apx-agent agents list` / topology / watchdog. On by default when a UC name + model are configured; skips with a notice otherwise. See [apps-uc-registry-shim-design.md](../engine-scope/apps-uc-registry-shim-design.md) |
| **Governance (UC permissions on the artifact)** | Strong — the registered model has UC ACLs | Improved by the shim — the UC version manifest carries UC ACLs, but the *running* surface is still the App, governed by App-level permissions |
| **Cost model (idle)** | Cheap when scaled to zero; you pay for cold starts | Cheap when scaled to zero (auto-suspend); cold start is faster |
| **Cost model (busy)** | DBU per compute-hour at the endpoint sku | DBU per compute-hour at the App's compute |
| **Deletion semantics** | `databricks serving-endpoints delete` — endpoint and its versions go | `databricks apps delete` — removes the App; bundle artifact remains in workspace files |
| **Logs surface** | Model Serving logs UI; CLI: `databricks serving-endpoints query-logs` | App logs UI; CLI: `databricks apps logs <app>` |
| **Local-dev parity** | Approximate — `apx-agent agents run` runs FastAPI, but the ChatAgent wrapper is bypassed | High — `apx-agent agents run` wraps `uvicorn agent_server.start_server:app`, the same command `databricks.yml` runs in prod |

### Pick **Apps** when

- You're in the dev loop and Model-Serving container builds are hurting iteration.
- You want to ship a UI alongside the agent (Lakebase + React + agent in one App).
- You need async, WebSocket, background workers, or multiple routes per agent.
- Your governance model is fine with App-level permissions instead of UC ACLs on a registered model.

### Pick **Model Serving** when

- You need traffic split / canary at the platform layer.
- You're publishing to a Mosaic AI Supervisor agent.
- The Review App workflow is part of your release process.
- Your security review needs UC ACLs on the model artifact, not just the App.
- The artifact must be reachable from another endpoint or job that resolves UC models, not URLs.

## 2. Why both exist

Honestly: two product teams shipped two solutions, in two different years,
solving overlapping problems. Databricks hasn't reconciled them.

**Model Serving inherited the classical-ML model-serving pipeline.** Each
deploy = build a Docker image with the model artifact + Python runtime, push
it to the registry, promote to a serving endpoint, warm the route. This
pipeline is rock-solid for classical ML models that change once a week. For
agents that change ten times a day, the build queue becomes the bottleneck.
ChatAgent was retrofitted onto this pipeline so agents could ride the same
infrastructure as `sklearn` models.

**Apps came from the web-app side.** It's the platform's answer to "I want
to run an arbitrary FastAPI app inside the Databricks security boundary,
with workspace auth and ambient credentials." When that platform existed,
running an agent inside one became the obvious shortcut — no container
build, no model registry, just push code and restart. `mlflow.genai.agent_server`
is the thin glue that makes the FastAPI app speak the ResponsesAgent
contract.

Both will exist for a while. Apps doesn't have a story for canary, traffic
split, or Supervisor integration, and those won't ship inside Apps before
they ship in Model Serving. Model Serving doesn't have a story for fast
iteration, async work, or co-located UI, and those aren't a Model Serving
problem to solve.

## 3. apx-agent's role

You write **one** Agent + tools + resources. apx-agent compiles to either
target based on `--target` at deploy time. The migration guide from
`databricks/app-templates` becomes irrelevant because there's nothing to
migrate — both targets read the same agent definition.

```
                      ┌───────────────────────────────────────┐
                      │  Agent + tools + resources (your code)│
                      └─────────────────┬─────────────────────┘
                                        │
                ┌───────────────────────┴────────────────────────┐
                │                                                │
                ▼                                                ▼
   compile_to_chat_agent(agent, …)              compile_to_responses_agent(agent, …)
   └─► mlflow.pyfunc.log_model                   └─► @invoke()/@stream() decorators
       └─► register UC, deploy endpoint              └─► AgentServer FastAPI app
           apx-agent agents deploy --target model-serving             apx-agent agents deploy --target apps
```

A single agent definition compiles to either target. Switching is a CLI flag,
not a rewrite.

## 4. Code-level differences hidden by apx-agent

These are the surface-level differences between the two contracts. apx-agent
absorbs each of them so your `Agent` definition doesn't change.

### Contract: ChatAgent vs ResponsesAgent

- Model Serving expects an `mlflow.pyfunc.ChatAgent` with `predict()` /
  `predict_stream()` methods.
- Apps expects functions decorated with `mlflow.genai.agent_server.invoke()` /
  `stream()`, taking `ResponsesAgentRequest` and returning
  `ResponsesAgentResponse` / `ResponsesAgentStreamEvent`.

apx-agent picks the right wrapper per compile target — same `Agent`, two
output shapes.

### Identity: `customInputs.user_token` vs `X-Forwarded-Access-Token`

- Model Serving: callers thread the user's OBO token into
  `custom_inputs["user_token"]`; the ChatAgent extracts it inside `predict()`
  and rebuilds the per-request `WorkspaceClient`.
- Apps: the platform injects the user's token as the `X-Forwarded-Access-Token`
  HTTP header on every request. The route reads it and threads it through.

apx-agent's wiring (see `apx_agent/_wiring.py` and `apx_agent/_invocations.py`)
normalizes both paths — the per-request `WorkspaceClient` and tool routing
work identically under either target, so tool code never has to ask which
runtime it's executing in.

### Resource declaration: MLflow `resources=[...]` vs `databricks.yml`

- Model Serving: resources are recorded on the MLflow run via
  `mlflow.pyfunc.log_model(..., resources=[...])`. The platform reads them
  at deploy time to provision the endpoint's permission grants.
- Apps: resources live in `databricks.yml` under
  `resources.apps.<name>.resources` (serving endpoints, databases, secrets,
  warehouses). The bundle deploy reconciles them; the App SP gets ambient
  credentials.

apx-agent projects a single `ResourceSpec` list into both shapes. You
declare resources once on the agent; the compile target picks the projection.

### Streaming envelope: ChatAgentChunk vs ResponsesAgentStreamEvent

- Model Serving: `predict_stream` yields `ChatAgentChunk(delta=...)`.
- Apps: `@stream()` yields `ResponsesAgentStreamEvent(...)`.

apx-agent's compiled wrappers translate the langgraph stream output into
the right envelope on each side. Your `Agent` doesn't see either type.

## 5. Migration

### Model Serving → Apps

When the build queue is hurting you and none of the Model-Serving-only
features are in your hot path:

```bash
# Old:
apx-agent agents deploy --module my_agent.app:agent \
           --model databricks-claude-sonnet-4-6 \
           --name main.agents.mine

# New — scaffold an editable Apps project alongside, then deploy:
apx-agent agents scaffold my_agent_apps --target apps
# Copy your existing agent.py contents into agent_server/agent.py
# (the Agent definition is byte-identical; only request entry points change).
apx-agent agents deploy --target apps
```

The `apx-agent agents scaffold --target apps` command lays down the file tree shown in
[`memory_demo`](../python/examples/memory_demo/) — `agent_server/`,
`scripts/`, `databricks.yml`, `pyproject.toml`. Drop your existing agent
module into `agent_server/agent.py`, wrap the entry points with the
`compile_to_responses_agent` + `@invoke()` / `@stream()` shape, and `apx
deploy --target apps` does the rest.

If you were doing per-request OBO via `custom_inputs["user_token"]`, the
Apps target handles the same thing automatically — Databricks Apps sets
`X-Forwarded-Access-Token` on every inbound request, and apx-agent's wiring
threads it into the compiled agent's `WorkspaceClient`. No code change.

### Apps → Model Serving

When you need canary or Supervisor publish:

```bash
# Re-target. Your agent module is reused as-is.
apx-agent agents deploy --target model-serving \
           --module agent_server.agent:agent \
           --name main.agents.mine
```

The `agent_server.agent` module already defines `agent` as a top-level
symbol; the Model-Serving compile path wraps it in a ChatAgent and logs to
MLflow. The `@invoke()` / `@stream()` decorations are inert under
Model Serving — they only register handlers when `mlflow.genai.agent_server`
runs, which it doesn't inside an MLflow model artifact.

### Running both targets in parallel

Nothing stops you. The same `agent_server.agent` module can be served by:

- An App registered as `memory-demo` (Apps target).
- A Model Serving endpoint registered as `main.agents.memory_demo`
  (Model Serving target).

Both will share the same agent definition, the same memory store, the same
tools. They'll diverge on UC permissions, request routing, and logging
surface — which is sometimes exactly what you want during a migration:
keep the old endpoint serving production while the new App soaks new
traffic.

## 6. Operational notes

A few things that bite in practice:

- **Apps don't survive workspace files cleanup.** A `databricks workspace
  delete --recursive` on the bundle's root path will leave the App pointing
  at a missing source_code_path. The App keeps serving the last good
  snapshot, but redeploys fail until you re-push. Model Serving endpoints
  are insulated from this — the artifact lives in UC.
- **Apps + Lakebase = same security boundary.** When the App has a database
  resource declared, the App SP can mint Postgres credentials directly via
  the Databricks SDK. Model-Serving endpoints can do the same, but the
  pattern is less common — see [`lakebase-recipe.md`](../running/lakebase-recipe.md)
  for the wiring.
- **Trace export is identical.** Both targets emit MLflow traces to the
  experiment recorded in `MLFLOW_EXPERIMENT_NAME` / `MLFLOW_EXPERIMENT_ID`.
  Switching targets does not change your eval / observability setup.
- **The Apps target ships uvicorn config in `databricks.yml`** — the
  `command:` block sets the worker count, host, and port. Tune workers
  based on expected concurrency; Apps does not autoscale uvicorn worker
  count for you.

## 7. Cheat sheet

| You want… | Run |
|---|---|
| Deploy a new agent to Apps | `apx-agent agents scaffold X && cd X && apx-agent agents deploy --target apps` |
| Deploy a new agent to Model Serving | `apx-agent agents scaffold X --target model-serving && cd X && apx-agent agents deploy --target model-serving --name <uc-three-part>` |
| Move an existing agent from Model Serving to Apps | `apx-agent agents scaffold X_apps --target apps`, then drop agent module in, then `cd X_apps && apx-agent agents deploy --target apps` |
| Move an existing agent from Apps to Model Serving | `apx-agent agents deploy --target model-serving --module agent_server.agent:agent --name <uc-three-part>` |
| Validate Apps bundle without deploying | `databricks bundle validate --target dev --profile <profile>` |
| Tail Apps logs | `databricks apps logs <app-name> --profile <profile>` |
| Tail Model Serving logs | `databricks serving-endpoints query-logs <endpoint>` |

## 8. Cross-links

- [`memory_demo` worked example](../../python/examples/memory_demo/README.md) —
  identical agent code, both targets, side-by-side.
- [`lakebase-recipe.md`](../running/lakebase-recipe.md) — durable state on Postgres
  for either runtime.
- [`troubleshooting.md`](./troubleshooting.md) — per-runtime
  failure modes.
