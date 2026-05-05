# **Data Triage Agent — Setup Guide**

An AI agent that investigates why data is missing from Databricks tables or APIs. Describe the problem in plain language; the agent queries the relevant systems, traces lineage, and returns a plain-language explanation of where and why data was dropped.

**Live demo:** https://mcp-data-triage-7474652869938903.aws.databricksapps.com

———

## **Prerequisites**

| **Requirement** | **Details** |
| --- | --- |
| **Python** | 3.11+ |
| **uv** | Python package manager — `curl -LsSf https://astral.sh/uv/install.sh \ |
| **APX** | Databricks App framework — install from internal source (ask Stuart if you don't have it) |
| **Databricks CLI** | pip install databricks-cli or brew install databricks/tap/databricks |
| **Databricks workspace** | With the following enabled (see below) |

### **Workspace requirements**
1. **Foundation Model API (FMAI)** — A serving endpoint for the LLM. Currently configured for databricks-claude-sonnet-4-6. If your workspace uses a different model endpoint, update the model field in pyproject.toml under [tool.apx.agent].
2. **Unity Catalog system tables** — The agent queries system.access.table_lineage to trace data flow. This must be enabled in your workspace (Workspace Settings → Unity Catalog → System Tables).
3. **SQL warehouse** — The agent auto-discovers the first available serverless warehouse. Any active SQL warehouse will work.

———

## **Setup**

### **1. Clone and install**

cd data-triage-agent

uv sync

### **2. Configure environment**

Create a .env file at the project root:

DATABRICKS_CONFIG_PROFILE=<your-profile-name>

UV_NATIVE_TLS=1

Replace <your-profile-name> with your Databricks CLI profile. Set one up with:

databricks configure --profile <your-profile-name>

UV_NATIVE_TLS=1 works around corporate PyPI SSL certificate issues — safe to include regardless.

### **3. Run locally**

apx dev

The agent UI opens at http://localhost:8000. APX handles the FMAI tool-calling loop, streaming, and OAuth.

### **4. Deploy to your workspace**

apx deploy

The deployed app uses OBO (on-behalf-of) tokens — the agent queries Databricks as the user who invokes it, respecting their Unity Catalog permissions.

———

## **What's inside**

data-triage-agent/

├── pyproject.toml                  # App config: name, model endpoint, entrypoint

├── databricks.yml                  # Databricks Asset Bundle config

├── app.yml                         # Databricks App runtime command

├── PRD.md                          # Product requirements and investigation flow

├── README.md                       # Architecture and tool reference

├── SETUP.md                        # This file

├── src/data_triage_agent/

│   └── backend/

│       ├── agent_router.py         # All 9 tools defined here

│       ├── app.py                  # FastAPI app entry point

│       ├── router.py               # Version and user endpoints

│       ├── models.py               # Pydantic models

│       └── core/                   # APX framework (agent loop, config, deps)

└── .gitignore

———

## **Tools**

| **Tool** | **Status** | **What It Does** |
| --- | --- | --- |
| run_sql_query | Working | Execute read-only SQL against any Databricks table |
| get_table_info | Working | Schema, row count, data freshness for a given table |
| get_table_lineage | Working | Upstream sources via system.access.table_lineage |
| find_jobs_for_table | Working | Which jobs write to a given table (via lineage) |
| get_job_run_history | Working | Recent run history — success/failure, timestamps |
| get_job_run_logs | Working | Error output from a specific failed run |
| get_job_source_paths | Working | Notebook/file paths for the tasks in a job |
| read_github_file | Stubbed | Read a source file from a GitHub repo (Phase 2) |
| search_github_code | Stubbed | Search for patterns across a repo (Phase 2) |

### **Activating GitHub tools (Phase 2)**

Create a Databricks secret scope with a read-only GitHub PAT:

databricks secrets create-scope github --profile <your-profile>

databricks secrets put-secret github token --string-value <PAT> --profile <your-profile>

The PAT needs repo:read access on the pipeline and API repos. Then uncomment the implementations in agent_router.py and redeploy.

———

## **Customizing the model**

The model endpoint is set in pyproject.toml:

[tool.apx.agent]

model = "databricks-claude-sonnet-4-6"

Change this to any FMAI-served model available in your workspace (e.g., databricks-meta-llama-3-3-70b-instruct).

———

## **Example queries**

Try these to verify the agent is working:
- *"What tables exist in the gold catalog?"*
- *"Show me the lineage for catalog.schema.my_table"*
- *"Are there any failed jobs that write to catalog.schema.my_table?"*
- *"Account 12345 isn't showing up in the demand-response API. Can you investigate?"*

———

## **Questions?**

Contact Stuart Gano (Databricks) — stuart.gano@databricks.com

## Jira Async Trigger Setup

To enable automatic investigation when a "Data Issue" Jira ticket is created:

### 1. Jira Service Account

Create or designate a Jira user with "Add Comments" permission on the target project. This is the account the agent posts comments as.

### 2. API Token

Log in as the service account → **Account Settings → Security → API tokens → Create**.

Copy the token — it's shown only once.

### 3. Issue Type

Confirm "Data Issue" exists as an issue type in the target Jira project. If not, a project admin can create it under **Project Settings → Issue types**.

### 4. Webhook Registration

In the target Jira project: **Project Settings → Webhooks → Create webhook**.

| Field | Value |
|---|---|
| URL | `https://<data-triage-app-url>/webhook/jira` |
| Events | Issue → created |
| JQL filter | `issuetype = "Data Issue"` |
| Secret | Value of `JIRA_WEBHOOK_SECRET` (see below) |

### 5. Environment Variables

Generate a webhook secret:

```bash
openssl rand -hex 32
```

Set these environment variables in the app and job:

| Variable | Where to set | Value |
|---|---|---|
| `JIRA_BASE_URL` | App env + job cluster | `https://your-org.atlassian.net` |
| `JIRA_SERVICE_ACCOUNT_EMAIL` | App env + job cluster | Service account email |
| `JIRA_API_TOKEN` | App env + job cluster | Token from step 2 |
| `JIRA_WEBHOOK_SECRET` | App env only | Generated secret |
| `DATA_TRIAGE_JOB_ID` | App env only | Job ID from `databricks bundle deploy` output |

For local testing, add these to `.env` (gitignored).

For Databricks Apps deployment, set `JIRA_WEBHOOK_SECRET` and `DATA_TRIAGE_JOB_ID` as app environment variables in the Databricks Apps UI or via `databricks.yml`.

### 6. Deploy and Get Job ID

```bash
databricks bundle deploy --profile <your-profile>
```

The output includes the job ID:

```
✓  data-triage-investigation (job ID: 1234)
```

Set `DATA_TRIAGE_JOB_ID=1234` in the app's environment variables.
