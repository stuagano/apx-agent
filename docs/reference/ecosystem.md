# Ecosystem

How apx-agent relates to other tools in the Databricks AI space.

## Official Databricks projects

| Project | Relationship |
|---------|--------------|
| [databrickslabs/mcp](https://github.com/databrickslabs/mcp) | Managed MCP endpoints for Genie, UC functions, vector search. apx-agent exposes your *own* tools over MCP (Apps mode); these expose Databricks platform capabilities as MCP. |
| [databricks-solutions/custom-mcp-databricks-app](https://github.com/databricks-solutions/custom-mcp-databricks-app) | Reference for hosting a custom MCP server on Databricks Apps. apx-agent is the full-featured pattern — agent loop, A2A discovery, hub registration, dev UI. |
| [databricks-solutions/genierails](https://github.com/databricks-solutions/genierails) | Configures Genie spaces (row filters, masks, guardrails). Orthogonal: use genierails to configure the spaces that `genie_tool()` calls at runtime. |

## Community projects

| Project | Relationship |
|---------|--------------|
| [alexxx-db/databricks-genie-mcp](https://github.com/alexxx-db/databricks-genie-mcp) | Genie spaces over MCP. apx-agent's `genie_tool()` covers the same ground natively; the MCP version is useful in non-apx clients. |
| [RafaelCartenet/mcp-databricks-server](https://github.com/RafaelCartenet/mcp-databricks-server) | UC metadata over MCP. Prior art for apx-agent's `catalog_tool` / `lineage_tool` / `schema_tool`. |
