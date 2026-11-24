---
name: databricks-genie
description: "Declare, promote and query Databricks Genie Spaces (Genie Agents) with apx-agent. Use when creating a Genie Space, exporting or importing one, migrating it between workspaces or environments, or giving an agent a tool that asks a Genie Space questions."
---

# Databricks Genie

Declare Genie Spaces as code with `apx-agent resources genie-space`, and query
them from an agent with the `apx_agent` Genie tools.

## Overview

A Genie Space (the Management API's name for a Genie Agent) connects Unity
Catalog tables to a natural-language interface: it turns questions into SQL,
runs them on a SQL warehouse, and answers conversationally.

## When to Use This Skill

- Creating a Genie Space, or changing its tables, instructions, sample
  questions or benchmarks
- Exporting a space's configuration for review, backup or migration
- Cloning a space, or promoting it from dev to staging to prod
- Giving an apx-agent agent a tool that asks a Genie Space questions

## Manage spaces as code

Keep each space in a YAML (or JSON) file in the repo and drive it with
`apx-agent resources genie-space`:

```bash
apx-agent resources genie-space pull <space-id> genie/sales.yml --profile dev   # export a live space
apx-agent resources genie-space validate genie/sales.yml                         # offline checks
apx-agent resources genie-space plan genie/sales.yml --profile dev               # create / update + diff / noop
apx-agent resources genie-space push genie/sales.yml --profile dev --dry-run     # plan only
apx-agent resources genie-space push genie/sales.yml --profile dev               # create or update
apx-agent resources genie-space push genie/sales.yml --profile prod --warehouse <prod-warehouse-id>  # promote
```

| Step | Command |
|------|---------|
| Create | Write the file (or `pull` an existing space), then `push` |
| Export / backup | `pull <space-id> FILE` |
| Update | Edit the file, `plan`, then `push` |
| Clone in one workspace | Copy the file, change `title`, drop its `deployments` entry, `push` |
| Migrate / promote | `push FILE --profile <target> [--warehouse <id>]` |
| Delete | Not supported: `push` never deletes. Trash the space in the workspace |

`push` finds the space by the ID recorded under `deployments` for the target
workspace, then by `title`, and otherwise creates it. Full file format,
validation rules and promotion notes:
[docs/deploy/genie-agents-as-code.md](../../../../docs/deploy/genie-agents-as-code.md).
Writing the file: [spaces.md](spaces.md).

## Query a space from an agent

```python
from apx_agent import Agent, genie_tool, genie_query_tool

agent = Agent(
    instructions="Answer data questions with the Sales Genie Space.",
    tools=[
        genie_tool("<space-id>", description="Ask the Sales Genie Space a question."),
        genie_query_tool("<space-id>", name="query_sales_rows"),  # rows + generated SQL
    ],
)
```

- `genie_tool` returns Genie's text answer; `genie_query_tool` returns
  `sql_results`, `generated_sql` and `genie_response`.
- Both run as the calling user (OBO) and declare the `genie_space` resource.
- Follow-up questions in one apx session continue the same Genie conversation.
  apx deletes the conversations it started when `end_genie_session(session_id)`
  is called, or when they go idle; `apx-agent resources genie-sweep` clears
  leftovers.

Details: [conversation.md](conversation.md).

## Prerequisites

1. **Tables in Unity Catalog** — silver/gold tables with descriptive column
   names and comments
2. **SQL warehouse** — its ID goes in the file's `warehouse_id` (override per
   workspace with `push --warehouse`)
3. **A Databricks CLI profile** per target workspace

### Creating Tables

Use these skills in sequence:
1. `databricks-synthetic-data-gen` - Generate raw parquet files
2. `databricks-spark-declarative-pipelines` - Create bronze/silver/gold tables

## Reference Files

- [spaces.md](spaces.md) - Writing, updating and promoting a space config file
- [conversation.md](conversation.md) - Querying spaces with the apx-agent Genie tools

## Related Skills

- **[databricks-agent-bricks](../databricks-agent-bricks/SKILL.md)** - Use Genie Spaces as agents inside Supervisor Agents
- **[databricks-apx-agent](../databricks-apx-agent/SKILL.md)** - Build and deploy the agent that calls Genie
- **[databricks-synthetic-data-gen](../databricks-synthetic-data-gen/SKILL.md)** - Generate raw parquet data to populate tables for Genie
- **[databricks-spark-declarative-pipelines](../databricks-spark-declarative-pipelines/SKILL.md)** - Build bronze/silver/gold tables consumed by Genie Spaces
- **[databricks-unity-catalog](../databricks-unity-catalog/SKILL.md)** - Manage the catalogs, schemas, and tables Genie queries
