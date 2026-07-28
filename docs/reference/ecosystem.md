# Ecosystem

How apx-agent relates to other tools in the Databricks AI space.

## Companion: compliance posture

| Project | Relationship |
|---------|--------------|
| [stuagano/databricks-watchdog](https://github.com/stuagano/databricks-watchdog) | **Compliance posture** — cross-domain policies, violation lifecycle, owner accountability (including a `data_quality` domain). apx-agent is the **agent runtime** (tools, OBO, hooks, deploy); Watchdog decides allow / reject / redact. Wire them with `WatchdogGuard` — see [compliance.md](compliance.md). Pipeline DQ frameworks such as [DQX](https://databrickslabs.github.io/dqx/) feed Watchdog posture, not the agent SDK. |

```text
apx-agent  = agent runtime (tools, OBO, hooks, Apps / Serving deploy)
watchdog   = compliance posture (policies, violations, data_quality domain)
DQX        = Spark pipeline quality checks → signals for Watchdog, not apx
```

## Official Databricks projects

| Project | Relationship |
|---------|--------------|
| [databrickslabs/mcp](https://github.com/databrickslabs/mcp) | Managed MCP endpoints for Genie, UC functions, vector search. apx-agent exposes your *own* tools over MCP (Apps mode); these expose Databricks platform capabilities as MCP. |
| [databricks-solutions/custom-mcp-databricks-app](https://github.com/databricks-solutions/custom-mcp-databricks-app) | Reference for hosting a custom MCP server on Databricks Apps. apx-agent is the full-featured pattern — agent loop, A2A discovery, hub registration, dev UI. |
| [databricks-solutions/genierails](https://github.com/databricks-solutions/genierails) | Configures Genie spaces (row filters, masks, guardrails). Orthogonal: use genierails to configure the spaces that `genie_tool()` calls at runtime. |
| [databrickslabs/dqx](https://github.com/databrickslabs/dqx) | PySpark data-quality checks (profile, quarantine, Lakeflow). Orthogonal to apx: run DQX in pipelines; surface posture via Watchdog’s `data_quality` domain rather than bundling `DQEngine` into the agent process. |

## Community projects

| Project | Relationship |
|---------|--------------|
| [alexxx-db/databricks-genie-mcp](https://github.com/alexxx-db/databricks-genie-mcp) | Genie spaces over MCP. apx-agent's `genie_tool()` covers the same ground natively; the MCP version is useful in non-apx clients. |
| [RafaelCartenet/mcp-databricks-server](https://github.com/RafaelCartenet/mcp-databricks-server) | UC metadata over MCP. Prior art for apx-agent's `catalog_tool` / `lineage_tool` / `schema_tool`. |
