# Deployment

apx-agent compiles the same agent to either runtime. Pick by workload; swap with one CLI flag. The full decision table is in [apps-vs-model-serving.md](apps-vs-model-serving.md).

## Model Serving (Mosaic AI) — `--target model-serving`

The default. `compile_to_chat_agent` produces an MLflow `ChatAgent` with declared resources; `log_agent` registers it in Unity Catalog; `databricks.agents.deploy` promotes it to a serving endpoint. Recognized natively by AI Playground, Review App, Agent Evaluation, MLflow tracing, and Supervisor Agent as a sub-agent.

```bash
apx-agent agents deploy --model databricks-claude-sonnet-4-6 \
           --name main.agents.my_agent
```

- **Pay-per-request** — scale-to-zero, no idle cost
- **Identity passthrough** automatic from Playground / Genie / Supervisor (via `customInputs.user_token`)
- **Stateless** — request/response only; no persistent state between calls
- **Production patterns** — canary deploys (`apx-agent canary deploy/promote/rollback`), traffic-split, hot-swap LLM (`apx-agent agents hot-swap`)
- **Container build** — 5–30 min on first deploy; subsequent deploys reuse cached layers
- **Best for** — production agents the platform routes traffic to (Supervisor sub-agents, AI Playground, Knowledge Assistants)

## Databricks Apps — `--target apps`

The MLflow GenAI Agent Server path. `compile_to_responses_agent` produces a `ResponsesAgent` with `@invoke` / `@stream` decorated functions; `databricks bundle deploy + bundle run` pushes code and restarts the app. No container build. The deploy is a code push.

```bash
apx-agent agents scaffold my_agent --target apps   # writes an editable project directory
cd my_agent && uv sync
apx-agent agents deploy --target apps              # builds wheel, auto-resolves MLflow
                                                   # experiment, bundle deploy + run
```

- **Code-push deploy** — seconds to minutes from edit to running app
- **Identity passthrough** automatic via `X-Forwarded-Access-Token` injected by the Apps runtime
- **Stateful** — in-memory caches, background loops, websockets, custom UI all work
- **Async-native** — `@invoke()` and `@stream()` decorators support `async def`
- **Best for** — dev loop, agents with co-located UI, durable workflows, anything that benefits from fast iteration

The legacy `create_app(agent)` FastAPI wrapper still works for Apps hosting that doesn't go through MLflow GenAI Server — useful when you want apx-agent's full host (OBO middleware, `/mcp` MCP server, `/.well-known/agent.json` discovery card, hub auto-registration, dev UI at `/_apx/*`) without the bundle deploy flow.

## Verified live deploys

Worked examples deployed to a real workspace as Databricks Apps:

- [`python/examples/memory_demo/`](../python/examples/memory_demo/) — single agent with in-memory memory store
- [`python/examples/customer_triage/`](../python/examples/customer_triage/) — `HandoffAgent` over 4 sub-agents with memory wired into the account specialist

Memory recall verified working across the HandoffAgent boundary — principal-keyed memory survives sub-agent transitions because the key is the user, not the session.
