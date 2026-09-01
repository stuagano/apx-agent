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

A peer reached via `sub_agents=[url]` becomes a callable delegate tool named after its card (`peer-agent` → `peer_agent`). If a local tool already owns that name, the local tool keeps it and the peer is **neither advertised nor callable** — a startup warning names the collision, and the fix is to rename one of them (#636). Advertised capability and callable tool always agree.

## App-to-app authentication

When sub-agents are deployed as sibling Apps (not Model Serving endpoints),
**auth is at the Databricks Apps SSO gateway**, not a second in-process
protocol (#631):

1. The caller authenticates to the callee App (bearer / SSO). Without
   credentials the gateway rejects before the agent process sees the request.
2. **CAN_USE permission** on the callee app for the caller's SP. Without it,
   the gateway returns 401.
3. Each app has a service principal (platform-created). M2M credentials
   authenticate outbound calls.
4. **FMAPI uses the callee app's own identity.** When app A calls app B, B's
   internal LLM calls use B's own SP token, not A's.

Each App keeps its own persistent platform-created service principal. An App
family may share `CAN_USE`/`CAN_MANAGE` group policy, never credentials.

The A2A JSON-RPC surface is `POST /` on the App. Inside the Apps runtime,
apx-agent **also fails closed** when a request reaches that handler with
neither `X-Forwarded-Access-Token` nor `Authorization: Bearer` — a belt-and-
suspenders check that the gateway (or a mis-mounted path) did not drop
identity. Local `apx-agent run` stays open for the solo-dev loop. Operators
that intentionally serve A2A without gateway identity set
`APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true` (same opt-in as G2 / Discover).

Tool/MCP/dev-UI routes under `/api/` (`api_prefix`) also accept bearer tokens
via the gateway; `/invocations` and `/responses` mount only at their natural
paths (no `/api/` mirror).

For APX Apps deployments, declare the peer's exact HTTPS Apps URL in
`sub_agents` and deploy with an explicit `--profile`. Before mutation, APX
lists Apps under that profile and requires exactly one URL match with an App
ID, name, and URL. Zero or multiple matches fail closed. It then emits the
resolved App **name** as a native bundle resource with `CAN_USE`; operators do
not need to hand-maintain a matching permission patch or App ID. The
authorization summary shows the resolved name and immutable ID without
credentials.

This automatic reconciliation is additive only: it preserves existing bundle
resources and permissions and never deletes or downgrades grants. The legacy
`--auto-update-yml` flag remains accepted, but it no longer gates this work.

**Common pitfalls:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| 302 redirect (HTML login page) | SSO gateway intercepted an unauthenticated call | Send a bearer token; A2A JSON-RPC is `POST /` on the App URL |
| 401 Unauthorized (gateway) | Caller lacks `CAN_USE` on callee | Verify the declared URL resolves uniquely and redeploy with the intended explicit profile; inspect the authorization summary and resulting bundle permission |
| 401 from apx-agent on Apps (`#631`) | Request reached `POST /` without proxy/bearer headers | Call through the Apps gateway (or set `APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK=true` only if intentional) |
| FMAPI 401 inside sub-agent | Callee service identity lacks its required model/resource permission | Declare the callee's service resource and redeploy; the caller's OBO token is not reused for the callee's LLM call |

For user-scoped multi-agent across boundaries, prefer a Model Serving deployment when the
Databricks caller supplies identity passthrough. Apps still require explicit app-to-app OAuth
and `CAN_USE` authorization; do not assume an Apps gateway call becomes user-scoped automatically.

Apps-hosted agents can also be routed through a Mosaic AI Supervisor directly: `apx-agent supervisor add --app <app-name>` registers the App as an `app`-type supervisor tool (requires databricks-sdk >= 0.120).
