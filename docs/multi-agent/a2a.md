# A2A discovery + app-to-app auth

## A2A discovery

Apps-hosted agents publish `/.well-known/agent.json` with capabilities, skills, and MCP endpoint:

```json
{
  "name": "data_triage_agent",
  "description": "Investigate why data is missing from Databricks tables",
  "url": "https://data-triage-agent.workspace.databricksapps.com",
  "skills": [
    {"name": "get_table_lineage", "description": "Get upstream sources..."},
    {"name": "find_jobs_for_table", "description": "Which jobs write to a table..."}
  ],
  "mcpEndpoint": "https://data-triage-agent.workspace.databricksapps.com/mcp"
}
```

On Model Serving, UC + the Mosaic AI registry are the equivalent discovery surface.

## App-to-app authentication

When sub-agents are deployed as sibling Apps (not Model Serving endpoints), the orchestrator's calls go through the Databricks Apps SSO gateway:

1. **Routes under `/api/`** accept bearer tokens; other routes trigger interactive SSO. apx-agent mounts every endpoint at both its natural path and an `/api/` mirror.
2. **Each app has a service principal.** The platform creates one automatically. M2M credentials authenticate outbound calls.
3. **CAN_USE permission** on the callee app for the caller's SP. Without it, the gateway returns 401.
4. **FMAPI uses the app's own identity.** When app A calls app B, B's internal LLM calls use B's own SP token, not A's.

```bash
# 1. Mint an OAuth secret for each app's SP
databricks api post /api/2.0/accounts/servicePrincipals/<SP_ID>/credentials/secrets \
  --profile <profile> --json '{}'

# 2. Set credentials in each app.yaml
env:
  - name: DATABRICKS_CLIENT_ID
    value: "<app-sp-client-id>"
  - name: DATABRICKS_CLIENT_SECRET
    value: "<secret-from-step-1>"

# 3. Grant the orchestrator's SP CAN_USE on each sub-agent app
databricks api patch /api/2.0/permissions/apps/<sub-agent-name> \
  --profile <profile> --json '{
    "access_control_list": [{
      "service_principal_name": "<orchestrator-sp-client-id>",
      "permission_level": "CAN_USE"
    }]
  }'
```

**Common pitfalls:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| 302 redirect (HTML login page) | Calling `/responses` instead of `/api/responses` | Use `/api/` prefix for programmatic calls |
| 401 Unauthorized | Caller's SP lacks CAN_USE on callee | Grant via permissions API |
| FMAPI 401 inside sub-agent | Sub-agent using caller's OBO for LLM calls | Set `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` on the sub-agent |
| `invalid_client` on M2M | Wrong SP secret (app recreated, SP changed) | Mint a new secret for the current SP |

For user-scoped multi-agent across boundaries, prefer Model Serving deployment — the platform handles identity passthrough automatically, no SP-to-SP dance.
