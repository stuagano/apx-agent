# Data Triage Agent

Investigates why data is missing from Databricks tables — **six-step `SequentialAgent` pipeline, each step structurally guaranteed to run**. Deploys to **either** Databricks Apps **or** Mosaic AI Model Serving via `apx deploy --target {apps,model-serving}`.

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

> **Also required:** The [data-inspector](../data-inspector/) sub-agent must be deployed (or running locally) before running this agent. Deploy it first and note its URL — you'll set it as `DATA_INSPECTOR_URL`.

---

## Part 1: Workspace setup (one-time)

### Step 1: Deploy data-inspector first

This agent delegates SQL and Delta forensics to data-inspector via A2A. You need data-inspector running before starting data-triage-agent. Follow [../data-inspector/README.md — Part 3](../data-inspector/README.md#part-3-deploy-to-databricks-apps) to deploy it and note the public URL.

For local development only, you can run it locally on port 9000 instead of deploying — see [Part 2](#part-2-local-development) below.

### Step 2: (Optional) Note your Genie space ID

The `genie_agent` step queries a Genie space for domain context. To use it:

1. In the Databricks UI, go to **AI/BI → Genie**
2. Open the Genie space you want to query
3. Copy the space ID from the URL: `https://<workspace>/genie/spaces/<SPACE_ID>`

You'll set this as `GENIE_SPACE_ID` in your `.env` (local) or in `databricks.yml` variables (deployed). It's optional — the pipeline skips the Genie step if no space is configured.

### Step 3: (Optional) GitHub personal access token

The `code_agent` step reads source notebooks and Python files from GitHub to look for filter or logic bugs. To use it:

1. Go to **GitHub → Settings → Developer settings → Personal access tokens**
2. Create a token with `repo:read` scope (read-only access to source code)

You'll set this as `GITHUB_TOKEN` in your `.env`.

---

## Part 2: Local development

### Step 1: Install

```bash
cd examples/data-triage-agent
uv sync
```

### Step 2: Configure your Databricks CLI profile

```bash
databricks configure --profile my-workspace
# enter workspace URL and personal access token when prompted

databricks current-user me --profile my-workspace
# should return your user info
```

### Step 3: Create a `.env` file

```env
DATABRICKS_CONFIG_PROFILE=my-workspace

# Required: point to a running data-inspector (local or deployed)
DATA_INSPECTOR_URL=http://localhost:9000

# Optional: Genie space for domain context (Step 2 above)
# GENIE_SPACE_ID=abc123

# Optional: GitHub access for source code inspection (Step 3 above)
# GITHUB_TOKEN=ghp_...
```

> `.env` is gitignored. Never commit it.

### Step 4: Run the tests

All tests mock external dependencies — no live Databricks connection needed:

```bash
uv run pytest tests/ -v
```

Expected:

```
tests/test_config.py::test_settings_loads_from_env PASSED
tests/test_investigate.py::test_extract_query_all_fields PASSED
tests/test_jira_client.py::... PASSED
tests/test_webhook.py::... PASSED
```

### Step 5: Run locally (two-terminal setup)

Start data-inspector first (terminal 1):

```bash
cd ../data-inspector
DATABRICKS_CONFIG_PROFILE=my-workspace uv run uvicorn data_inspector.backend.app:app --port 9000 --reload
```

Then start data-triage-agent (terminal 2):

```bash
cd ../data-triage-agent
DATA_INSPECTOR_URL=http://localhost:9000 \
DATABRICKS_CONFIG_PROFILE=my-workspace \
uv run uvicorn data_triage_agent.backend.app:app --port 8001 --reload
```

The chat interface opens at `http://localhost:8001`. Try:

```
Why is catalog.schema.daily_summary missing data for 2024-03-15?
The events table has no rows after midnight — what happened?
```

---

## Part 3: Deploy

The agent ships with two deploy paths. Pick by workload; the full tradeoff write-up is in [`docs/apps-vs-model-serving.md`](../../../docs/apps-vs-model-serving.md).

### Option A: Databricks Apps (`--target apps`, recommended for fast iteration)

Code-push deploy via `databricks bundle deploy + bundle run`. No container build. The `agent_server/` package wraps the existing `pipeline:agent` with `compile_to_responses_agent` and registers `@invoke()` / `@stream()` handlers.

```bash
cd python/examples/data-triage-agent
apx deploy --target apps --var "data_inspector_url=https://<your-data-inspector-app>.databricksapps.com"
```

`apx deploy --target apps` is the complete pipeline — builds the
apx-agent wheel, stages `.build/`, auto-resolves an MLflow experiment id
(creates/reuses `/Users/<you>/mcp-data-triage-agent-dev`), runs
`databricks bundle deploy + run`, polls until `RUNNING`/`ACTIVE`.

The `--var data_inspector_url=...` plumbs the deployed data-inspector
sub-agent URL into the pipeline's A2A handoff.

Verify:

```bash
databricks apps get mcp-data-triage-agent --profile <p> -o json \
  | jq '{url, app_status: .app_status.state, compute_status: .compute_status.state}'
```

When both states are `RUNNING` / `ACTIVE`, invoke it:

```bash
APP_URL=$(databricks apps get mcp-data-triage-agent --profile <p> -o json | jq -r .url)
TOKEN=$(databricks auth token --profile <p> | jq -r .access_token)
curl -X POST "$APP_URL/invocations" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":"why is main.gold.orders empty?"}]}'
```

### Option B: Model Serving (`--target model-serving`, default)

For production endpoints recognized by AI Playground, Review App, Supervisor Agent. Container build path.

```bash
apx deploy --module data_triage_agent.backend.pipeline:agent \
           --model databricks-claude-sonnet-4-6 \
           --name main.agents.data_triage_agent
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
