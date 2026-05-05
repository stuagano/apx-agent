# Data Triage Agent

Investigates why data is missing from Databricks tables or downstream APIs.

## What it does

Runs a six-step investigation pipeline: confirms what data is missing, traces Unity Catalog lineage, checks job run history and error logs, queries Genie Spaces for domain context, inspects source code for filter logic, and synthesizes a root cause report. Delegates SQL queries and Delta forensics to a companion `data-inspector` sub-agent.

For non-investigation queries (table discovery, general SQL), routes to a general agent that also delegates to the data-inspector.

## Required env vars

| Variable | Description |
|---|---|
| `DATA_INSPECTOR_URL` | Base URL of the deployed `data-inspector` companion agent |
| `AGENT_HUB_URL` | (optional) URL of the Agent Hub to register with on startup |

## Deploy to Databricks Apps

```bash
# 1. Build the wheel
uv build --wheel

# 2. Upload and deploy (see apx-agent docs for full deploy workflow)
apx deploy --profile <your-profile>
```

## Tools

| Tool | Description |
|---|---|
| `run_sql_query` | Execute a read-only SQL query |
| `get_table_info` | Schema, row count, and freshness for a UC table |
| `get_table_lineage` | Upstream sources via Unity Catalog lineage |
| `find_jobs_for_table` | Jobs that write to a given table |
| `get_job_run_history` | Recent run history for a job |
| `get_job_run_logs` | Error output from a failed run |
| `get_job_source_paths` | Notebook/file paths for a job |
| `list_genie_spaces` | List available Genie Spaces |
| `query_genie_space` | Ask a question to a Genie Space |
| `read_github_file` | Read a source file from GitHub (stub — configure your GitHub token) |
| `search_github_code` | Search for code patterns in GitHub (stub) |
