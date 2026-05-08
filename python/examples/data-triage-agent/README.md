# Data Triage Agent

Investigates why data is missing from Databricks tables — **six-step `SequentialAgent` pipeline, each step structurally guaranteed to run**.

A `SequentialAgent` composes six focused `Agent`s that each handle one phase of the investigation. Conversation history accumulates automatically — every agent's output is visible to the next. Delegates SQL and Delta forensics to the [data-inspector](../data-inspector/) sub-agent via A2A.

---

## What makes this simple

Six focused agents, each with a narrow tool set, wired into a guaranteed pipeline:

```python
investigation_pipeline = SequentialAgent(
    agents=[
        presence_agent,   # Is the data actually missing?
        lineage_agent,    # What feeds this table?
        pipeline_agent,   # Did the upstream jobs fail?
        genie_agent,      # What does domain context say?
        code_agent,       # Is there a filter bug in the source?
        synthesis_agent,  # Root cause report
    ]
)
```

Each step is a plain `Agent` — no custom orchestration logic, no retry loops:

```python
presence_agent = Agent(
    tools=[run_sql_query, get_table_info],
    sub_agents=[data_inspector_url],   # Delta forensics via A2A
    instructions="You are the Data Presence Investigator. ...",
)
```

This replaces a single agent with a long checklist. Instead of hoping the LLM follows every step, each step is guaranteed by the pipeline structure.

---

## Pipeline

```
"Why is my table missing data from yesterday?"
           │
           ▼
   Step 1 — Data Presence    Is the data actually missing? Row counts, date ranges.
           │
           ▼
   Step 2 — Lineage Trace    What upstream tables and jobs feed this table?
           │
           ▼
   Step 3 — Pipeline Check   Did the upstream jobs run? Any failures in the logs?
           │
           ▼
   Step 4 — Genie Context    What does domain knowledge say about this data?
           │
           ▼
   Step 5 — Code Inspection  Is there a filter, join, or logic bug in the source?
           │
           ▼
   Step 6 — Synthesis        Root cause report with evidence from all five steps.
```

---

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Databricks CLI | `pip install databricks-cli` or `brew install databricks/tap/databricks` |
| Databricks workspace | SQL warehouse, Unity Catalog lineage enabled, Genie space (optional) |

> **Also required:** The [data-inspector](../data-inspector/) sub-agent must be deployed before running this agent. Deploy it first and note its URL — you'll set it as `DATA_INSPECTOR_URL`.

---

## Run locally

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/data-triage-agent

uv sync

# Point to a running data-inspector (local or deployed)
DATA_INSPECTOR_URL=http://localhost:9000 \
uv run uvicorn data_triage_agent.backend.app:app --port 8001
```

In a separate terminal, start the data-inspector:

```bash
cd ../data-inspector
uv run uvicorn data_inspector.backend.app:app --port 9000
```

Try it:

```
Why is catalog.schema.daily_summary missing data for 2024-03-15?
The events table has no rows after midnight — what happened?
```

---

## Deploy to Databricks Apps

### Prerequisites

- **Databricks CLI** — [install](https://docs.databricks.com/dev-tools/cli/databricks-cli.html)
- **uv** — `pip install uv`
- A Databricks workspace with [Apps enabled](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html)
- **data-inspector deployed** — get its URL first

### 1. Authenticate

```bash
databricks auth login --host https://<your-workspace>.azuredatabricks.net
databricks current-user me
```

### 2. Get the code

```bash
git clone https://github.com/stuagano/apx-agent
cd python/examples/data-triage-agent
```

### 3. Configure `app.yml`

Set the data-inspector URL:

```yaml
env:
  DATA_INSPECTOR_URL: "https://<your-data-inspector-app>.databricksapps.com"
```

### 4. Build

```bash
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
```

### 5. Deploy

```bash
databricks bundle deploy
```

Check status:

```bash
databricks apps get mcp-data-triage -o json | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('URL:   ', d.get('url', 'not yet available'))
print('State: ', d.get('app_status', {}).get('state', 'unknown'))
"
```

### Redeploy after changes

```bash
uv build --wheel -o .build/
ls .build/*.whl | xargs basename > .build/requirements.txt
databricks bundle deploy
```

---

## Configuration

| Env var | Required | Description |
|---------|----------|-------------|
| `DATA_INSPECTOR_URL` | Yes | Base URL of the deployed data-inspector sub-agent |
| `AGENT_HUB_URL` | No | Register with an Agent Hub on startup |

---

## Tools

| Tool | Step | What it does |
|------|------|--------------|
| `run_sql_query` | 1 | Execute a read-only SQL query |
| `get_table_info` | 1 | Schema, row count, and freshness |
| `get_table_lineage` | 2 | Upstream sources via Unity Catalog lineage |
| `find_jobs_for_table` | 2 | Jobs that write to a given table |
| `get_job_run_history` | 3 | Recent run history for a job |
| `get_job_run_logs` | 3 | Error output from a failed run |
| `get_job_source_paths` | 3 | Notebook/file paths for a job |
| `list_genie_spaces` | 4 | List available Genie Spaces |
| `query_genie_space` | 4 | Ask a question to a Genie Space |
| `read_github_file` | 5 | Read source file from GitHub |
| `search_github_code` | 5 | Search for code patterns in GitHub |
| `data_inspector` | 1–3 | Sub-agent: Delta bisect, version diff, audit log |

---

## Project Structure

```
data-triage-agent/
├── app.yml                            # Databricks Apps runtime config + env vars
├── databricks.yml                     # Asset Bundle — build, deploy, app + job resources
└── src/data_triage_agent/backend/
    ├── agent_router.py                # All tool functions
    ├── pipeline.py                    # SequentialAgent — six-step investigation pipeline
    └── app.py                         # FastAPI app
```
