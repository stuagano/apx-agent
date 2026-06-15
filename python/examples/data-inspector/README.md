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
def delta_bisect(
    table: str,
    condition: str,
    version_lo: int = -1,
    version_hi: int = -1,
    ws: Workspace = None,
) -> dict:
    """Binary search Delta history to find when data matching condition appeared."""
    ...
```

Other agents call this via A2A at `<url>/mcp` by adding it to `sub_agents=["<url>"]`.

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | Unity Catalog access, SQL warehouse |

---

## Part 1: Workspace setup (one-time)

This agent discovers tables dynamically — there is no infrastructure to pre-create. All you need is CLI access to a workspace with Unity Catalog and at least one SQL warehouse.

### Step 1: Verify CLI profile and warehouse access

```bash
databricks configure --profile my-workspace
# enter workspace URL and personal access token when prompted

databricks current-user me --profile my-workspace
# should return your user info

databricks warehouses list --profile my-workspace
# confirm at least one warehouse is listed
```

That's it. The agent needs no tables created ahead of time — it discovers everything at runtime via the Unity Catalog APIs.

---

## Part 2: Local development

### Step 1: Install

```bash
cd examples/data-inspector
uv sync
```

### Step 2: Configure your Databricks CLI profile

```bash
databricks configure --profile my-workspace
# enter workspace URL and personal access token when prompted

databricks current-user me --profile my-workspace
# should return your user info
```

### Step 3: Set your profile for local development

No `.env` file is needed — the workspace client is injected automatically by Databricks Apps in production. For local development, set your profile via environment variable:

```bash
export DATABRICKS_CONFIG_PROFILE=my-workspace
```

You can also prefix each `uv run` command:

```bash
DATABRICKS_CONFIG_PROFILE=my-workspace uv run uvicorn app:app --port 9000 --reload
```

### Step 4: Run the tests

This example doesn't include automated tests. Run the agent locally and try queries:

```bash
DATABRICKS_CONFIG_PROFILE=my-workspace uv run uvicorn app:app --port 9000 --reload
```

Then open `http://localhost:9000` and try:

```
What tables are in my main catalog?
Show me the schema for catalog.schema.events
When did rows with status='ERROR' first appear in catalog.schema.events?
Who last modified catalog.schema.accounts and when?
```

### Step 5: Run locally

The agent runs on port 9000 — this is the default used when the [data-triage-agent](../data-triage-agent/) calls it as a sub-agent:

```bash
DATABRICKS_CONFIG_PROFILE=my-workspace uv run uvicorn app:app --port 9000 --reload
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Review `app.yml`

No env vars need to be set — the workspace client is injected automatically and there are no required configuration values:

```yaml
# app.yml — no changes required for a basic deployment
command: ["uvicorn", "app:app", "--workers", "2"]
```

### Step 2: Deploy

```bash
uv run apx-agent agents deploy
```

### Step 3: Verify

```bash
databricks apps get mcp-data-inspector --profile my-workspace -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

When `State: RUNNING`, copy the URL — you'll pass it to data-triage-agent as `DATA_INSPECTOR_URL`:

```python
# In data-triage-agent, pass this URL as an environment variable:
DATA_INSPECTOR_URL=https://<your-app>.databricksapps.com
```

The MCP endpoint is live at `<url>/mcp`. Any other agent can add this as a sub-agent:

```python
agent = Agent(tools=[...], sub_agents=["https://<data-inspector-url>"])
```

---

## Configuration

No env vars required — the workspace client is injected by Databricks Apps.

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
├── agent.py                      # Agent definition
├── tools.py                      # all 10 tools
├── app.py                        # FastAPI app (uvicorn target: app:app)
├── agent_server/
│   └── start_server.py           # FastAPI entry point
├── app.yml                       # Databricks Apps runtime config
└── databricks.yml                # Asset Bundle — build, deploy, app resource
```
