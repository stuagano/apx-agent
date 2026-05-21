# Jira integration (opt-in)

Wires data-triage-agent into a Jira workflow: when a "Data Issue" ticket is
created, Jira fires a webhook at `/webhook/jira` on the deployed app, the
router validates the HMAC signature and triggers a Databricks Job
(`data_triage_job_id`) via `ws.jobs.run_now`. The job runs the investigation
asynchronously and posts the result back as a comment on the ticket — this
avoids Jira's 30s webhook timeout while still giving reporters a self-serve
"automated first pass" before a human picks it up.

This integration is **opt-in**. The agent runs fine without it — `app.py`
imports the webhook router, but if `JIRA_WEBHOOK_SECRET` isn't set, the
endpoint returns 401 on every request and nothing in the rest of the agent
depends on it. Delete this directory if you don't want it.

## Files

| File | What it does |
|------|--------------|
| `webhook.py` | FastAPI router mounted at `/webhook/jira`. HMAC verify + dispatch to job. |
| `jira_client.py` | Thin HTTP client for posting comments back to Jira (ADF body format). |
| `config.py` | Pydantic settings: `jira_*`, `data_triage_job_id`. Loaded from `.env`. |
| `jobs/investigate.py` | Databricks Job entrypoint. Runs the FM-API tool-calling loop and posts the result via `JiraClient`. |
| `tests/` | Unit tests for all four modules (mocked — no live Jira or Databricks). |

## Setup

1. Create a Jira webhook pointing at `https://<your-app-url>/webhook/jira`
   with a shared HMAC secret.
2. Create the Databricks Job (one task, `python_file =
   integrations/jira/jobs/investigate.py`) and note its `job_id`.
3. Set in your `.env` (or the app's env vars):
   ```
   JIRA_BASE_URL=https://your-org.atlassian.net
   JIRA_SERVICE_ACCOUNT_EMAIL=bot@your-org.com
   JIRA_API_TOKEN=...
   JIRA_WEBHOOK_SECRET=...        # same value configured on the Jira side
   DATA_TRIAGE_JOB_ID=12345
   ```
4. Run `uv run pytest integrations/jira/tests/ -v` to confirm the
   integration tests pass.
