# Deployment

apx-agent deploys agents to Databricks Apps (`--target apps`).

New to the Apps/Agent Bricks runtime? Read [How the Agent Bricks platform works](agentbricks-platform.md) first — it explains the durable-agent model, the CLI, deploy, the hosting tiers (scale-to-zero vs dedicated-instance), and the identity model that everything below builds on.

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
- **Scale-to-zero is tier-dependent** — only an **App Space** deploy (`LIQUID`) idles to zero; a plain/dedicated-instance Apps deploy is `MEDIUM` and always-on. See [agentbricks-platform.md](agentbricks-platform.md).

The legacy `create_app(agent)` FastAPI wrapper still works for Apps hosting that doesn't go through MLflow GenAI Server — useful when you want apx-agent's full host (OBO middleware, `/mcp` MCP server, `/.well-known/agent.json` discovery card, hub auto-registration, dev UI at `/_apx/*`) without the bundle deploy flow.

## Verified live deploys

Worked examples deployed to a real workspace as Databricks Apps:

- [`python/examples/memory_demo/`](../python/examples/memory_demo/) — single agent with in-memory memory store
- [`python/examples/customer_triage/`](../python/examples/customer_triage/) — `HandoffAgent` over 4 sub-agents with memory wired into the account specialist

Memory recall verified working across the HandoffAgent boundary — principal-keyed memory survives sub-agent transitions because the key is the user, not the session.
