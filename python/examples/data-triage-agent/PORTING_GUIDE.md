# Porting Guide — Data Triage Agent → Uplight Environment

For Drew Hylbert (or whoever ports this). The reference implementation runs on fe-stable. This is a recipe for landing it in an Uplight Databricks workspace with Uplight's data and Uplight's source code.

## What you get from the reference implementation

Everything in `agents/data-triage-agent/` and `agents/data-inspector/` is portable. The fixture data in fe-stable (`serverless_stable_qh44kx_catalog.explain_my_bill.*`) is just for demo purposes — you replace it with real Uplight tables.

Two apps, one architecture:
- **data-inspector** — sub-agent. Reusable. Owns Delta forensics, schema, row counts, version history. Worth keeping as its own app so other agents (contract parsing, entity resolution, Self-Service MCP) can reuse it.
- **data-triage-agent** — composes the 6-step investigation pipeline. Calls data-inspector via A2A.

## Step-by-step port

### 1. Pick your workspace and catalogs

Decide which Uplight Databricks workspace this lands in. The agent uses these system tables — verify they're accessible to the workspace's app service principal:
- `system.access.table_lineage` — required (lineage tools)

Decide which Uplight catalogs the agent should query. The agent doesn't filter by catalog — it queries whatever the user passes. So no code change unless you want to scope it.

### 2. Auth + service principal

The deployed app uses on-behalf-of (OBO) auth — it queries Databricks as the user who hits the agent. That means the user needs:
- Read access to the tables they're investigating
- Read access to the jobs/pipelines they're tracing
- Ability to call SQL warehouses

Set up a SQL warehouse. The agent auto-discovers the first serverless warehouse via `_get_warehouse_id()`. If you want to pin a specific one, set the `WAREHOUSE_ID` env var (you'd need to add this to the code — currently it's auto-discovery only).

### 3. GitHub integration (currently stubbed)

In `src/data_triage_agent/backend/agent_router.py`, the `read_github_file` and `search_github_code` tools return stubs:

```python
def read_github_file(repo: str, path: str, ws: Workspace) -> dict[str, Any]:
    return {"stub": True, "message": f"GitHub not yet configured. Would read {repo}/{path}"}
```

To wire to Uplight's GitHub:
1. Add `pygithub` to `pyproject.toml` dependencies
2. Add a `GITHUB_TOKEN` env var to `app.yml` (use a service account or fine-grained PAT)
3. Replace the stub with:

```python
from github import Github
import os

def read_github_file(repo: str, path: str, ws: Workspace) -> dict[str, Any]:
    gh = Github(os.environ["GITHUB_TOKEN"])
    file = gh.get_repo(repo).get_contents(path)
    return {
        "repo": repo,
        "path": path,
        "content": file.decoded_content.decode("utf-8")[:10000],
        "sha": file.sha,
    }
```

The investigation pipeline's "Code Inspector" step (step 5) will start producing real results.

### 4. Genie Spaces

The agent calls `ws.genie.list_spaces()` and queries them dynamically. If Uplight has Genie Spaces configured for AMI / billing / customer data, they'll surface automatically.

If you want to constrain which Spaces the agent can use, edit the `query_genie_space` tool to enforce a list.

### 5. Replace the fixture data with real data

The eval cases reference the demo schema. In Uplight:
- Pick a real table you can demo against (e.g. one of your AMI rollup tables)
- Update `eval_cases.md` with realistic queries against that table
- Update `eval/eval_dataset.py` for regression testing

The agent code itself doesn't need to change — it queries whatever table the user names.

### 6. Deploy commands

In your Uplight workspace:

```bash
# In each agent directory
echo "DATABRICKS_CONFIG_PROFILE=<uplight-profile>" > .env
uv sync
apx build
# IMPORTANT: app names MUST start with "mcp-" for Genie Code discovery.
# pyproject.toml [tool.apx.metadata].app-name and databricks.yml resources.apps.<key>.name
# both need to use the mcp- prefix. The bundle will create the app on first deploy.
databricks apps create mcp-data-inspector --description "..." --profile <uplight-profile>
databricks apps start mcp-data-inspector --profile <uplight-profile>
# wait for compute ACTIVE
apx deploy --skip-build

# Get the data-inspector URL
DI_URL=$(databricks apps get mcp-data-inspector --profile <uplight-profile> | jq -r .url)

# In data-triage-agent dir, update app.yml with $DI_URL, then:
apx build
databricks apps create mcp-data-triage --description "..." --profile <uplight-profile>
databricks apps start mcp-data-triage --profile <uplight-profile>
apx deploy --skip-build
```

### 6a. Required gotchas — read this before you deploy

These are the five things that turned a half-day deploy into a full day for me. None are documented in apx-agent README; do them once and you're set.

**1. App name must start with `mcp-`.**
Genie Code discovers MCP servers by app-name prefix. Apps not named `mcp-*` won't show up. Update both `pyproject.toml` (`[tool.apx.metadata].app-name`) and `databricks.yml` (`resources.apps.<key>.name`) to use the prefix. If you create the app manually via `databricks apps create` first, the bundle will fail with "App with same name already exists" — delete the manually-created app and let the bundle create it.

**2. Auth shim — apx-agent default conflicts on Databricks Apps.**
The apx-agent `Dependencies.Workspace` calls `WorkspaceClient(token=..., host=f"https://{X-Forwarded-Host}")`. Two problems on Databricks Apps:
- The OBO `token=` plus env-injected `DATABRICKS_CLIENT_ID/SECRET` triggers SDK error: `more than one authorization method configured: oauth and pat`
- `X-Forwarded-Host` is the *app's* URL, not the *workspace's* URL — so SDK calls hit dead endpoints (e.g. `<app>/api/2.1/unity-catalog/catalogs` 404s)

Override `Dependencies` in `<agent>/backend/core/__init__.py`:

```python
from typing import Annotated, TypeAlias
from apx_agent import create_app as create_app
from apx_agent._defaults import HeadersDependency
from databricks.sdk import WorkspaceClient
from fastapi import Depends


def _get_obo_workspace_client(headers: HeadersDependency) -> WorkspaceClient:
    if headers.token:
        return WorkspaceClient(
            token=headers.token.get_secret_value(),
            auth_type="pat",  # disambiguates OAuth-vs-PAT
            # NOTE: do NOT pass host=  — let SDK use env DATABRICKS_HOST (workspace URL).
            # X-Forwarded-Host is the app URL, which is wrong for SDK calls.
        )
    return WorkspaceClient()


_OboClientDep: TypeAlias = Annotated[WorkspaceClient, Depends(_get_obo_workspace_client)]


class Dependencies:
    UserClient: TypeAlias = _OboClientDep
    Workspace: TypeAlias = _OboClientDep
```

Then update every `from apx_agent import Dependencies` to `from .core import Dependencies` so your tools use the shimmed version. Both `pipeline.py` and `router.py` need this fix.

**3. OAuth scopes — must be explicitly configured.**
A new app's `effective_user_api_scopes` defaults to just `iam.current-user:read` and `iam.access-control:read`. UC API calls (catalogs/schemas/tables) and SQL queries will 403 until you set scopes. Bundle deploys reset scopes — apply them after every `apx deploy` if you don't pin them in the bundle.

```bash
cat > /tmp/scopes.json <<'EOF'
{"user_api_scopes": [
  "sql",
  "dashboards.genie",
  "workspace.workspace",
  "files.files",
  "vectorsearch.vector-search-endpoints",
  "serving.serving-endpoints"
]}
EOF
databricks apps update mcp-data-inspector --profile <uplight-profile> --json @/tmp/scopes.json
databricks apps update mcp-data-triage --profile <uplight-profile> --json @/tmp/scopes.json
```

Note: `unity-catalog`, `jobs`, `iam.*` are NOT valid scope names in this API (despite what `effective_user_api_scopes` returns). UC and most workspace APIs go through `workspace.workspace`; SQL queries go through `sql`. After updating scopes, users have to re-authorize the app (incognito or hard refresh) to pick up the new OBO token.

**4. CORS — required for Genie Code to connect.**
Genie Code is browser-based. Cross-origin POST to `/mcp` is rejected without explicit CORS. Add CORSMiddleware in `app.py`:

```python
from fastapi.middleware.cors import CORSMiddleware

app = create_app(agent)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.databricks\.com",
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)
```

Per the [Databricks docs](https://docs.databricks.com/aws/en/genie-code/mcp): *"If you run into CORS errors, you may need to add your workspace URL to your app's list of allowed origins."*

**5. Stateless HTTP at `/mcp` is already provided.**
apx-agent ships `StreamableHTTPSessionManager(mcp_server, stateless=True)` mounted at `/mcp`. You don't need to add anything for the protocol itself — but the endpoint won't be reachable until #1-#4 are all in place. The endpoint Genie Code expects is exactly `https://<app-url>/mcp` — no `/sse`, no trailing slash.

Verify each one separately:
```bash
TOKEN=$(databricks auth token --profile <profile> | jq -r .access_token)
APP_URL=https://mcp-<name>-<wsid>.aws.databricksapps.com

# 1. App name starts with mcp- → discoverable
databricks apps get mcp-<name> --profile <profile> | jq .name

# 2. Auth shim → /api/current-user returns user JSON, not error
curl -sS -H "Authorization: Bearer $TOKEN" "$APP_URL/api/current-user"

# 3. Scopes → list_catalogs returns rows in the dev UI
curl -sS -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -X POST "$APP_URL/responses" -d '{"input":"call list_catalogs"}'

# 4. CORS → preflight returns access-control-allow-origin
curl -sS -i -X OPTIONS \
  -H "Origin: https://<your-workspace>.cloud.databricks.com" \
  -H "Access-Control-Request-Method: POST" \
  "$APP_URL/mcp" | grep -i access-control

# 5. MCP protocol → initialize returns server info
curl -sS -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -X POST "$APP_URL/mcp" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

If any of those fail, fix that one before moving on. Don't try to debug the whole stack at once — that's how I lost a day.

### 7. Custom investigation steps

If Uplight needs investigation steps the framework doesn't have (e.g. "check Cloud Composer DAG status"), add them. Pattern:

1. Add a tool function in `agent_router.py` (typed function, returns dict)
2. Add a new `Agent` block in `pipeline.py` with that tool
3. Insert it into the `SequentialAgent(agents=[...])` list at the right step

Each step is independent — adding/removing/reordering doesn't require deeper changes.

## What scales beyond the demo

These are the architectural moves that turn this from a demo into a platform:

1. **Self-Service MCP integration.** The triage agent could be exposed as a tool *to* Self-Service MCP (so other agents and Claude Code can call it). Add `from apx_agent import create_mcp_app` and ship a `/mcp/sse` endpoint. The Self-Service MCP proposal in the platform plan already accounts for this pattern.

2. **Cross-agent telemetry.** Each `apx-agent` ships traces to MLflow. Roll up across agents to see "what did this triage investigation actually cost / how long did it take / which tools did it call." Useful when you have 5+ agents in production.

3. **Failure-mode evals.** The eval framework supports negative cases. Add cases for things the agent should *refuse* (write queries, dangerous SQL, PII access without OBO grant). Run as a regression suite.

4. **Webhook integration with the support ticketing system.** When a ticket is created with certain tags, hit the agent via API and append the agent's first-pass diagnosis to the ticket. The agent has a `POST /responses` endpoint already — that's the integration point.

## Open questions for whoever ports

Best to think through before deploying to Uplight:

- Which workspace? Production-adjacent (real data, real risk) vs platform/dev (no real customer data, easier to iterate)?
- Who's the OBO identity for non-interactive callers? (Webhooks from ticketing won't have a user — need a service principal pattern.)
- Where do investigation transcripts go? MLflow traces by default — but if PII is in queries, audit retention matters.
- Tool surface ownership: does the data-triage-agent's tool list stay here, or do tools migrate into Self-Service MCP and the agent just calls MCP?

Worth a 30-min architecture call before the port if you want — happy to go through these with you.
