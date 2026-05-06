# Data Inspector

SQL queries, table schemas, and Delta forensics for Databricks tables — **standalone or sub-agent, under 200 lines of Python**.

A single `Agent` with 10 tools covering table discovery, SQL execution, and forensic Delta history analysis. Deploy standalone or expose via MCP so other agents can call it as a sub-agent.

---

## What makes this simple

One `Agent`, plain Python functions, injected workspace client:

```python
agent = Agent(
    tools=[
        list_catalogs, list_schemas, list_tables, search_tables,
        run_sql_query, get_table_info,
        delta_bisect, delta_bisect_column, version_diff, audit_lookup,
    ],
    instructions=SYSTEM_PROMPT,
)
```

Each tool receives a `WorkspaceClient` injected automatically — no auth wiring in your code:

```python
def delta_bisect(table: str, predicate: str, ws: Workspace = None) -> dict:
    """Binary search Delta history to find when data matching predicate appeared."""
    ...
```

Other agents call this via A2A at `<url>/mcp` by adding it to `sub_agents=["<url>"]`.

---

## Run locally

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/data-inspector

uv sync
uv run uvicorn data_inspector.backend.app:app --port 9000
```

Try it:

```
What tables are in my main catalog?
Show me the schema for catalog.schema.events
When did rows with status='ERROR' first appear in catalog.schema.events?
Who last modified catalog.schema.accounts and when?
```

---

## Deploy to Databricks Apps

### Prerequisites

- **Databricks CLI** — [install](https://docs.databricks.com/dev-tools/cli/databricks-cli.html)
- **uv** — `pip install uv`
- A Databricks workspace with [Apps enabled](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html)

### 1. Authenticate

```bash
databricks auth login --host https://<your-workspace>.azuredatabricks.net
databricks current-user me
```

### 2. Get the code

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/data-inspector
```

### 3. Build

```bash
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
```

### 4. Deploy

```bash
databricks bundle deploy
```

Check status:

```bash
databricks apps get mcp-data-inspector -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

When `State: RUNNING`, the MCP endpoint is live at `<url>/mcp`. Pass this URL to other agents as a sub-agent:

```python
agent = Agent(tools=[...], sub_agents=["https://<data-inspector-url>"])
```

### Redeploy after changes

```bash
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
databricks bundle deploy
```

---

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `AGENT_HUB_URL` | _(empty)_ | Optional: register with an Agent Hub on startup |

No other env vars required — the workspace client is injected by Databricks Apps.

---

## Tools

| Tool | What it does |
|------|--------------|
| `list_catalogs` | List accessible Unity Catalog catalogs |
| `list_schemas` | List schemas in a catalog |
| `list_tables` | List tables in a schema |
| `search_tables` | Search tables by name pattern |
| `run_sql_query` | Execute a read-only SQL query |
| `get_table_info` | Schema, row count, and data freshness |
| `delta_bisect` | Binary search Delta history to find when data appeared/disappeared |
| `delta_bisect_column` | Binary search for when a specific value appeared in a column |
| `version_diff` | Compare two Delta versions to see what changed |
| `audit_lookup` | Who changed a table and when via Unity Catalog audit log |

---

## Project Structure

```
data-inspector/
├── app.yml                       # Databricks Apps runtime config
├── databricks.yml                # Asset Bundle — build, deploy, app resource
└── src/data_inspector/backend/
    ├── agent_router.py           # Agent + all 10 tools
    └── app.py                    # FastAPI app
```
