# Data Inspector

SQL queries, table schemas, and Delta forensics for Databricks tables.

## What it does

Provides tools to inspect, query, and forensically analyze Delta tables in Unity Catalog. Useful as a standalone agent or as a sub-agent invoked by a higher-level triage agent.

## Required env vars

| Variable | Description |
|---|---|
| `AGENT_HUB_URL` | (optional) URL of the Agent Hub to register with on startup |

## Deploy to Databricks Apps

```bash
uv build --wheel
apx deploy --profile <your-profile>
```

## Tools

| Tool | Description |
|---|---|
| `run_sql_query` | Execute a read-only SQL query |
| `get_table_info` | Schema, row count, and freshness |
| `list_catalogs` | List accessible Unity Catalog catalogs |
| `list_schemas` | List schemas in a catalog |
| `list_tables` | List tables in a schema |
| `search_tables` | Search tables by name pattern |
| `delta_bisect` | Binary search Delta history to find when data appeared/disappeared |
| `delta_bisect_column` | Binary search for when a specific value appeared in a column |
| `version_diff` | Compare two Delta versions to see what changed |
| `audit_lookup` | Who changed a table and when via Unity Catalog audit log |
