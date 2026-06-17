# apx-agent documentation

- [positioning.md](positioning.md) — **where apx-agent fits**: what it's for (governed data agents on Databricks), what it's not (a coding-agent orchestrator), and how the layers compose

## Get Started

- [get-started/quickstart.md](get-started/quickstart.md) — install, scaffold, run locally, first deploy
- [get-started/migration.md](get-started/migration.md) — **Coming from ADK or OpenAI Agents SDK?** concept-mapping table and translation guide
- [get-started/cli.md](get-started/cli.md) — full CLI reference (`apx-agent agents scaffold`, `apx-agent agents run`, `apx-agent agents deploy`, `apx-agent eval`, ...)
- [get-started/dev-ui.md](get-started/dev-ui.md) — built-in tooling at `/_apx/*` (chat, traces, topology, probe)

## Agents

- [agents/overview.md](agents/overview.md) — agent types, key concepts, how to choose
- [agents/llm-agent.md](agents/llm-agent.md) — `LlmAgent` / `Agent` constructor, `run()`, `stream()`, `max_iterations`
- [agents/data-agent.md](agents/data-agent.md) — `DataAgent`: one line over a Unity Catalog schema
- [agents/coworker.md](agents/coworker.md) — `CoworkerAgent`: join two source systems on a shared key
- [agents/coworker-use-cases.md](agents/coworker-use-cases.md) — two-system join patterns (payroll, revenue ops, warranty, ...)
- [agents/composition.md](agents/composition.md) — `SequentialAgent`, `ParallelAgent`, `LoopAgent`, `agent_tool`
- [agents/routing.md](agents/routing.md) — `RouterAgent`, `HandoffAgent`

## Running agents

- [agents/llm-agent.md#running](agents/llm-agent.md#running) — `agent.run()`, `agent.stream()`, `max_iterations` safety cap
- [running/sessions-and-memory.md](running/sessions-and-memory.md) — session stores, memory stores, example stores
- [running/lakebase-recipe.md](running/lakebase-recipe.md) — Lakebase provisioning, pgvector, pool tuning
- [running/tracing.md](running/tracing.md) — automatic MLflow tracing, span schema, Delta export for analytics

## Tools

- [tools/overview.md](tools/overview.md) — governed primitives: `sql_tool`, `genie_tool`, `vector_search_tool`, `uc_function_tool`
- [tools/custom-tools.md](tools/custom-tools.md) — `@tool`, `http_tool`, `openapi_tool`, `mcp_tool`, `mcp_toolkit`
- [tools/mcp.md](tools/mcp.md) — Managed MCP gateway

## Multi-agent

- [multi-agent/overview.md](multi-agent/overview.md) — local vs remote sub-agents, durable execution, deploy boundaries
- [multi-agent/a2a.md](multi-agent/a2a.md) — A2A discovery and app-to-app auth

## Sessions and memory

- [running/sessions-and-memory.md](running/sessions-and-memory.md) — session stores, memory stores, `MemoryStore`, `ExampleStore`
- [running/lakebase-recipe.md](running/lakebase-recipe.md) — persistent pgvector memory on Lakebase

## Guardrails and safety

- [safety/callbacks.md](safety/callbacks.md) — `before_tool`, `before_model`, `before_agent_callback`, `after_*` hooks
- [safety/identity-passthrough.md](safety/identity-passthrough.md) — OBO token propagation; each caller runs as themselves
- [safety/compliance.md](safety/compliance.md) — Watchdog integration, audit log, `GuardrailsConfig`, `WatchdogGuard`

## Observability

- [running/tracing.md](running/tracing.md) — automatic MLflow tracing, `/_apx/traces` dev UI, `apx.*` span schema, Delta export
- [reference/cost-tracking.md](reference/cost-tracking.md) — `apx-agent agents cost`, `cost_for_agent`, DBU tracking

## Deploy

- [deploy/overview.md](deploy/overview.md) — deploy targets, compile flow, bundle structure
- [deploy/apps-vs-model-serving.md](deploy/apps-vs-model-serving.md) — decision table and tradeoffs
- [deploy/troubleshooting.md](deploy/troubleshooting.md) — common failure modes and fixes

## Evaluate

- [evaluate/overview.md](evaluate/overview.md) — MLflow evaluation, `apx-agent eval`, LLM-as-judge, `eval chain`

## Reference

- [reference/configuration.md](reference/configuration.md) — full `[tool.apx.agent]` field reference
- [reference/pyproject-toml.md](reference/pyproject-toml.md) — `pyproject.toml` shape
- [reference/cost-tracking.md](reference/cost-tracking.md) — `cost_for_agent`, `apx-agent agents cost` CLI
- [reference/ecosystem.md](reference/ecosystem.md) — ecosystem integrations
- [reference/hub.md](reference/hub.md) — apx Hub
- [reference/ci-smoke-test.md](reference/ci-smoke-test.md) — CI smoke test recipe

## Migration guide

Coming from Google ADK or OpenAI Agents SDK? See [get-started/migration.md](get-started/migration.md) for a concept-by-concept translation: `Agent`, `Runner`, `@function_tool`, guardrails, memory, sessions, handoffs, and Databricks-specific additions.
