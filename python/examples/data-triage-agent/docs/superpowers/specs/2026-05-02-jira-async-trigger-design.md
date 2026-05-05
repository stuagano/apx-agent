# Jira Async Trigger — Feature Design

## Goal

When a "Data Issue" ticket is created in Jira, the data-triage-agent automatically investigates and posts its findings as a comment on that ticket. No human needs to copy-paste the ticket into a chat window.

## Demo Audience

Drew Hylbert (Enterprise Architect) and Micaela Christopher (Engineering Manager) at Uplight. This demonstrates the full closed-loop story: external event → Databricks does work → result written back to the source system.

---

## Architecture

```
Jira: "Data Issue" ticket created
  → webhook POST /webhook/jira  (Databricks App)
  → validate HMAC-SHA256 signature
  → filter: issuetype == "Data Issue", event == "issue_created"
  → extract ticket fields
  → ws.jobs.run_now(DATA_TRIAGE_JOB_ID, notebook_params=ticket_fields)
  → return 202 immediately

  (minutes later, in the Databricks workspace)

  → Job task: jobs/investigate.py
  → build query from ticket fields
  → run create_investigation_pipeline(query)
  → format result as Jira wiki markup comment
  → POST comment to Jira REST API
  → job completes (visible + auditable in Databricks UI)
```

**Key property:** The Databricks App returns 202 before the investigation starts. Jira never blocks on a response. The job run appears in the Databricks workspace UI with full logs and output.

---

## Components

### `src/data_triage_agent/backend/webhook.py` — new

FastAPI router mounted at `/webhook/jira`.

- Validates `X-Hub-Signature` HMAC-SHA256 header against `JIRA_WEBHOOK_SECRET`
- Filters: `event == "jira:issue_created"` and `issue.fields.issuetype.name == "Data Issue"`
- Extracts fields from the Jira webhook payload:
  - `issue_key` (e.g. `DATA-42`)
  - `summary`
  - `description` (Atlassian Document Format → plain text, best-effort)
  - `priority` (name field)
  - `reporter` (displayName)
  - Any non-null custom fields (passed as a JSON string)
- Calls `ws.jobs.run_now(job_id=settings.data_triage_job_id, python_named_params={...})`
- Returns `202 Accepted` immediately
- On signature failure: returns 401 (Jira will retry; logs the failure)
- On non-matching events: returns 200 silently (Jira shouldn't send them but may)

### `src/data_triage_agent/jobs/investigate.py` — new

Standalone Python entrypoint for the Databricks Job task.

- Reads ticket fields from job parameters via `dbutils.widgets.get(key)` (set by `python_named_params` in `run_now`)
- Builds investigation query: `"{summary}\n\n{description}"` with priority and reporter appended if present
- Instantiates a Databricks workspace client (SDK picks up credentials from job context automatically)
- Calls `create_investigation_pipeline(DATA_INSPECTOR_URL)` and runs the investigation
- Formats output as Jira wiki markup (plain text with `{code}` blocks for SQL/stack traces)
- Calls `JiraClient.post_comment(issue_key, formatted_body)`
- On any exception: calls `JiraClient.post_comment(issue_key, error_comment)` so the ticket is never silently dropped

### `src/data_triage_agent/jira_client.py` — new

Thin httpx wrapper around the Jira REST API.

```python
class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str): ...
    def post_comment(self, issue_key: str, body: str) -> None: ...
```

- Auth: HTTP Basic with `email:api_token` (Jira Cloud standard)
- Endpoint: `POST /rest/api/3/issue/{issue_key}/comment`
- Body: `{"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]}}`
- Raises `JiraClientError` on non-2xx response

### `src/data_triage_agent/backend/config.py` — new (or extend existing settings)

Pydantic Settings class loaded from env vars + `.env`:

```python
class Settings(BaseSettings):
    jira_base_url: str
    jira_service_account_email: str
    jira_api_token: str
    jira_webhook_secret: str
    data_triage_job_id: int
    data_inspector_url: str = "http://localhost:9000"
```

### `databricks.yml` — modified

Add job resource so `databricks bundle deploy` creates it automatically:

```yaml
resources:
  jobs:
    data-triage-investigation:
      name: "data-triage-investigation"
      tasks:
        - task_key: investigate
          python_wheel_task:
            package_name: data_triage_agent
            entry_point: investigate
          job_cluster_key: investigation_cluster
      job_clusters:
        - job_cluster_key: investigation_cluster
          new_cluster:
            spark_version: "15.4.x-scala2.12"
            node_type_id: "m5d.large"
            num_workers: 0
            spark_conf:
              "spark.databricks.cluster.profile": "singleNode"
```

The job ID is output after first deploy and set as `DATA_TRIAGE_JOB_ID` in the app env config.

---

## Configuration

### New env vars

| Variable | Purpose | How to get |
|---|---|---|
| `JIRA_BASE_URL` | `https://uplight.atlassian.net` | Jira workspace URL |
| `JIRA_SERVICE_ACCOUNT_EMAIL` | Service account email | Uplight IT / Jira admin |
| `JIRA_API_TOKEN` | API token for that account | Jira → Account Settings → Security → API tokens |
| `JIRA_WEBHOOK_SECRET` | HMAC secret shared with Jira | Generate once (`openssl rand -hex 32`), set in both Jira and the app |
| `DATA_TRIAGE_JOB_ID` | Databricks Job ID to trigger | Output of `databricks bundle deploy` |

All existing env vars (`DATA_INSPECTOR_URL`) are unchanged.

### `.env.example` additions

```
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_SERVICE_ACCOUNT_EMAIL=data-triage-bot@your-org.com
JIRA_API_TOKEN=your_api_token_here
JIRA_WEBHOOK_SECRET=generate_with_openssl_rand_hex_32
DATA_TRIAGE_JOB_ID=123
```

---

## Jira Side Setup (one-time prerequisites for Drew's team)

1. **Service account**: Create or designate a Jira user with "Add Comments" permission on the target project
2. **API token**: Jira → that account → Account Settings → Security → Create API Token
3. **Issue type**: Confirm "Data Issue" exists as an issue type in the target project (or create it)
4. **Webhook registration**: Project Settings → Webhooks → Create:
   - URL: `https://<data-triage-app-url>/webhook/jira`
   - Events: Issue → created
   - Filter: `issuetype = "Data Issue"`
   - Secret: value of `JIRA_WEBHOOK_SECRET`

These steps are documented in `SETUP.md`.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Invalid HMAC signature | Return 401; log; Jira retries up to 3× then disables webhook |
| Non-matching event type | Return 200 silently |
| Job trigger fails (SDK error) | Return 500; Jira retries; log the error |
| Investigation fails mid-run | Job task catches exception, posts error comment to ticket |
| Jira comment POST fails | Logged; job task raises so the Databricks run shows as Failed (auditable) |

---

## Files Changed

```
src/data_triage_agent/backend/webhook.py     — new: webhook endpoint
src/data_triage_agent/backend/config.py      — new: Pydantic Settings
src/data_triage_agent/backend/app.py         — modified: include webhook router
src/data_triage_agent/jobs/__init__.py       — new: package marker
src/data_triage_agent/jobs/investigate.py    — new: job task entrypoint
src/data_triage_agent/jira_client.py         — new: Jira REST API client
databricks.yml                               — modified: add job resource
.env.example                                 — modified: add Jira vars
SETUP.md                                     — modified: Jira prerequisite steps
pyproject.toml                               — modified: add investigate entry_point
```

---

## Out of Scope

- Handling ticket updates or comments (only `issue_created` triggers investigation)
- Re-running investigations (user can manually trigger the job if needed)
- Jira field mapping / custom field schema (agent works with whatever text is in summary + description)
- Slack or email notifications in addition to Jira comment
