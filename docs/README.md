# apx-agent docs

| Section | Contents |
|---------|----------|
| [get-started/](get-started/) | Quickstart, CLI reference, Dev UI |
| [agents/](agents/) | LlmAgent, DataAgent, CoworkerAgent, composition, routing |
| [tools/](tools/) | Governed primitives, MCP |
| [multi-agent/](multi-agent/) | Deploy boundaries, sub-agents, A2A auth |
| [running/](running/) | Sessions, memory, Lakebase |
| [safety/](safety/) | Callbacks, identity passthrough, compliance |
| [deploy/](deploy/) | Apps vs Model Serving, deployment, troubleshooting |
| [evaluate/](evaluate/) | MLflow evaluation |
| [reference/](reference/) | Configuration, pyproject.toml, cost tracking, ecosystem |

## Get started

- [get-started/quickstart.md](get-started/quickstart.md) — prerequisites, scaffold, local run, first deploy
- [get-started/cli.md](get-started/cli.md) — full CLI surface
- [get-started/dev-ui.md](get-started/dev-ui.md) — `/_apx/*` tooling (chat, traces, topology, probe)

## Agents

- [agents/llm-agent.md](agents/llm-agent.md) — `LlmAgent` / `Agent` reference
- [agents/composition.md](agents/composition.md) — Sequential, Parallel, Loop, agent_tool
- [agents/routing.md](agents/routing.md) — RouterAgent, HandoffAgent
- [agents/data-agent.md](agents/data-agent.md) — `DataAgent` reference
- [agents/coworker.md](agents/coworker.md) — `CoworkerAgent` reference
- [agents/coworker-use-cases.md](agents/coworker-use-cases.md) — two-system join patterns

## Tools

- [tools/overview.md](tools/overview.md) — governed primitives: sql_tool, genie_tool, vector_search_tool, uc_function_tool
- [tools/mcp.md](tools/mcp.md) — Managed MCP gateway

## Multi-agent

- [multi-agent/overview.md](multi-agent/overview.md) — local vs remote, sub-agents, durable execution
- [multi-agent/a2a.md](multi-agent/a2a.md) — A2A discovery + app-to-app auth

## Running

- [running/sessions-and-memory.md](running/sessions-and-memory.md) — session stores, memory stores, example stores
- [running/lakebase-recipe.md](running/lakebase-recipe.md) — Lakebase provisioning, pgvector, pool tuning

## Safety

- [safety/callbacks.md](safety/callbacks.md) — lifecycle hooks
- [safety/identity-passthrough.md](safety/identity-passthrough.md) — OBO token propagation
- [safety/compliance.md](safety/compliance.md) — Watchdog, audit log, built-in guards

## Deploy

- [deploy/overview.md](deploy/overview.md) — deploy targets, compile flow
- [deploy/apps-vs-model-serving.md](deploy/apps-vs-model-serving.md) — decision table and tradeoffs
- [deploy/troubleshooting.md](deploy/troubleshooting.md) — common failure modes

## Evaluate

- [evaluate/overview.md](evaluate/overview.md) — MLflow evaluation, predict functions, chain eval

## Reference

- [reference/configuration.md](reference/configuration.md) — full `[tool.apx.agent]` field reference
- [reference/pyproject-toml.md](reference/pyproject-toml.md) — pyproject.toml shape
- [reference/cost-tracking.md](reference/cost-tracking.md) — cost_for_agent, CLI cost surface
- [reference/ecosystem.md](reference/ecosystem.md) — ecosystem integrations
- [reference/hub.md](reference/hub.md) — apx Hub
- [reference/ci-smoke-test.md](reference/ci-smoke-test.md) — CI smoke test recipe
