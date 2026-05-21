# Typed tools — for custom code

Define tools as functions with type annotations. The framework generates input schemas and descriptions from type hints and docstrings.

**Python** — type hints + docstrings, with `Dependencies.*` parameters injected by FastAPI:

```python
def get_jobs_for_table(table_full_name: str, ws: Dependencies.Workspace) -> list[dict]:
    """Find Databricks Jobs that write to a Unity Catalog table."""
    rows = run_sql(ws, f"SELECT job_id, name FROM system.lakeflow.jobs WHERE ...")
    return rows
```

**TypeScript** — Zod schemas + handler functions:

```typescript
const getJobs = defineTool({
  name: 'get_jobs_for_table',
  description: 'Find Databricks Jobs that write to a UC table',
  parameters: z.object({ tableName: z.string() }),
  handler: async ({ tableName, ws }) => { /* ... */ },
});
```

Custom tools can declare their own resources:

```python
from apx_agent import ResourceSpec, attach_resources

def query_orders(question: str, ws: Dependencies.Workspace) -> str:
    """Query the orders Delta table."""
    return run_sql(ws, f"SELECT ... FROM main.sales.orders WHERE ...")

attach_resources(query_orders, [ResourceSpec("uc_table", "main.sales.orders")])
```

`log_agent` picks these up the same way it picks up the platform factories.
