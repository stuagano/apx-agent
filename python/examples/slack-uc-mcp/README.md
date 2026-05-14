# slack-uc-mcp

> **Direction: Databricks → external service (Slack), as the calling user.**
>
> A Databricks agent (Genie, Agent Bricks, or a custom Python agent) reads Slack **as the calling user**, with per-user OAuth tokens stored and replayed by Unity Catalog.

> ## ⚠️ Private Preview
>
> This example uses the **UC u2m (user-to-machine) per-user connection** feature, currently in **Private Preview** on AWS, Azure, and GCP. Endpoints, JSON shapes, and CLI surfaces may change before GA. Contact your Databricks account team for preview access.
>
> Feedback on the preview goes to `agent-feedback@databricks.com`.

For the **opposite direction** (a Slack user invoking a slash command that runs a Databricks agent under their Databricks identity), see [slack-agent](../slack-agent/). The same UC u2m primitive *can* model that direction too — Slack user ID as `user_identity`, Databricks as the external service — but slack-agent intentionally implements it manually for clarity.

---

## What this example shows

The u2m connection pattern, end-to-end, with Slack as the worked example:

1. Admin creates one UC HTTP connection (`authType = u2m per user`) pointed at Slack
2. A service principal (SP) is the only identity granted on the connection
3. Each user goes through OAuth in your webapp once (Authorization Code + PKCE)
4. Your webapp posts `{authorization_code, pkce_verifier, redirect_uri}` to UC; **UC does the token exchange and stores access + refresh tokens internally**
5. The agent (running as the SP) calls Slack via `ws.serving_endpoints.http_request(conn=..., headers={"with-user-identity": user_id})`; UC vends the right user's token at call time

Tokens are never visible to the agent code or to end users. Refresh is automatic.

---

## Architecture

```
End user (Slack user ID = U123, or email)
   │
   ▼  OAuth Authorization Code + PKCE — in your webapp, not Databricks UI
[ Slack consent screen ] → authorization_code
   │
   ▼  webapp POSTs {auth_code, pkce_verifier, redirect_uri, user_identity=U123}
[ UC: /unity-catalog/connections/slack_mcp/user-credentials ]
   │  (caller = SP — the only principal granted on the connection)
   ▼  UC exchanges code → stores access + refresh tokens keyed by user_identity
[ UC managed credential store ]

later, at agent run time:

Agent (running as SP)
   │
   ▼  ws.serving_endpoints.http_request(conn="slack_mcp", headers={"with-user-identity": "U123"})
[ UC vends U123's Slack token, injects on the outbound call ]
   │
   ▼
Slack API — executes under U123's Slack permissions
```

The SP is the **governing entity**. Lock it down: no human should be able to assume it. Only the SP can read/write `user-credentials` on the connection.

---

## Why this pattern

| Concern | Custom OAuth/token code | UC u2m connection |
|---|---|---|
| OAuth flow initiation (redirect, PKCE verifier) | You write it | You write it |
| Token exchange (code → access_token) | You write it | UC does it |
| Token storage | Your code (dict, Delta, secret scope) | UC managed store |
| Token refresh | You write it | UC handles it |
| Replay at call time | Manual header injection | `with-user-identity` header |
| Token visible in app code | Yes | No — never exposed |
| Audit trail | Your logs | UC audit |
| Multi-user scoping | Manual mapping | Built-in (`user_identity` key) |
| Compatible with `mcp.slack.com/mcp` MCP server | Possible, but custom | Yes — `conn=` + path |

The trade-off: you still own the webapp that runs the OAuth dance (consent → callback). UC explicitly does **not** receive third-party webhooks. The webapp gets thinner by ~3 concerns (exchange, storage, refresh).

---

## Part 1: One-time setup (workspace admin)

### Step 1: Register a Slack app for the connection

At [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From Scratch**:

1. Under **OAuth & Permissions**, add the **user token scopes** you want agents to be able to use on each user's behalf — e.g. `channels:read`, `channels:history`, `groups:history`, `users:read`, `search:read`.
2. Add a **Redirect URL** pointing at *your webapp's* OAuth callback (not a Databricks URL): `https://{your-webapp}/oauth/slack/callback`.
3. Save the **Client ID** and **Client Secret**.

### Step 2: Create a service principal (the governing entity)

Account console → Service Principals → Create. Generate a client ID / secret pair you can give to the agent's runtime environment.

This SP will be the **only** principal granted on the UC connection. No human user should be able to authenticate as this SP — it exists solely to write/read user-credentials.

### Step 3: Create the UC u2m connection

In the workspace UI: **Catalog → External Data → Connections → Create connection**, with:

- **Connection type**: HTTP
- **Auth type**: u2m per user
- **Host / base URL**: `https://slack.com`
- **OAuth Client ID** / **Client Secret**: from the Slack app
- **OAuth Scopes**: matching the Slack app's user token scopes

Then grant **only the SP** on the connection — verify the exact GRANT syntax for SP principals against your workspace's preview docs:

```sql
-- syntax may differ in preview; the goal is: only the SP can USE CONNECTION
GRANT USE CONNECTION ON CONNECTION slack_mcp TO `<sp-application-id>`;
```

> This example targets Slack's REST API (`slack.com`). The same UC u2m primitive also works against Slack's hosted MCP server (`mcp.slack.com/mcp`) by setting `path=/mcp` and posting JSON-RPC payloads — different integration, same pattern. Pick one when you implement.

---

## Part 2: Per-user OAuth flow (in your webapp)

Your webapp owns the redirect dance. Three responsibilities:

1. Generate a PKCE `code_verifier` (43–128 random URL-safe chars) and `code_challenge` (`BASE64URL(SHA256(verifier))`).
2. Redirect the user to Slack's authorization URL with `client_id`, `redirect_uri`, `scope`, `state`, `code_challenge=...`, `code_challenge_method=S256`.
3. On callback, collect `authorization_code` from Slack, then **POST to UC** to store credentials.

`SP_TOKEN` below is a Databricks M2M OAuth access token minted from the SP's `client_id` / `client_secret` (use the `databricks-sdk` client_credentials flow or `databricks auth token` with an SP profile).

```bash
# Webapp callback handler — after receiving the auth code from Slack:
curl -X POST "https://${DATABRICKS_HOST}/ajax-api/2.1/unity-catalog/connections/slack_mcp/user-credentials" \
  -H "Authorization: Bearer ${SP_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @- <<JSON
{
  "connection_user_credential": {
    "user_identity": "${USER_ID}",
    "options_kvpairs": {
      "options": [
        {"key": "pkce_verifier",      "value": "${CODE_VERIFIER}"},
        {"key": "authorization_code", "value": "${AUTH_CODE}"},
        {"key": "oauth_redirect_uri", "value": "${REDIRECT_URI}"}
      ]
    }
  }
}
JSON
```

`user_identity` is **any string** that uniquely identifies the user in your system — Slack user ID, email, internal UUID, whatever you'll pass at call time. It does not need to be a Databricks principal.

The request must be made as the SP (governing entity). UC then exchanges the auth code for access + refresh tokens and stores them internally; nothing is returned to your webapp.

### Status / revoke

```bash
# Check credential status (no tokens returned — only expiry metadata)
GET  /ajax-api/2.1/unity-catalog/connections/slack_mcp/user-credentials/${USER_ID}

# Revoke
DELETE /ajax-api/2.1/unity-catalog/connections/slack_mcp/user-credentials/${USER_ID}
```

---

## Part 3: Consumption from an agent

The agent runs **as the SP**. It receives the calling user's identity from its own context (e.g. Databricks Apps' `X-Forwarded-User-Email`, or a query param) and passes it to UC as the `with-user-identity` header.

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ExternalFunctionRequestHttpMethod

# SP-authenticated client. This is the ONLY principal with access to the
# connection — humans cannot authenticate as it.
ws = WorkspaceClient(
    client_id=os.environ["SP_CLIENT_ID"],
    client_secret=os.environ["SP_CLIENT_SECRET"],
)

response = ws.serving_endpoints.http_request(
    conn="slack_mcp",
    method=ExternalFunctionRequestHttpMethod.POST,
    path="/api/conversations.history",          # or "/mcp" for the MCP server
    json={"channel": "C0123456"},
    headers={"with-user-identity": calling_user_id},
)
```

UC looks up `calling_user_id`'s stored Slack credentials, injects the user's access token onto the outbound call, and Slack enforces per-channel permissions for that user. If the user isn't in the channel, Slack returns `not_in_channel` — the SP's identity is irrelevant.

See [`agent_example.py`](./agent_example.py) for a runnable reference.

### Inside Genie / Agent Bricks

When the agent is a Databricks-native primitive (Genie space, Knowledge Assistant, MAS supervisor), wire the connection as a tool source in the UI. Genie / Agent Bricks already runs under an SP and forwards the calling user as `with-user-identity` automatically — no code.

---

## Verifying per-user scoping

1. User A (member of `#private-eng`) authorizes Slack via your webapp; the agent queries `#private-eng` with `with-user-identity: A` → returns messages.
2. User B (not in `#private-eng`) authorizes Slack via your webapp; the agent queries with `with-user-identity: B` → Slack returns `not_in_channel`.

If both users see the same data, the connection is configured as machine-to-machine instead of u2m — check the auth type.

---

## When not to use this

- **Slack-initiated flows** (a slash command in Slack). UC u2m connections are outbound from Databricks — UC does not receive Slack webhooks. You still need a webapp (Databricks App or otherwise) to handle the Slack event. See [slack-agent](../slack-agent/) for the manual reference.
- **Service-account / shared access** where per-user scoping is intentionally bypassed. Use a machine-to-machine connection (or a bot token in a secret scope) instead.
- **Production-critical workloads**, until this preview goes GA — API shapes can change.

---

## See also

- [slack-agent](../slack-agent/) — Slack → Databricks direction, manual reference (also benefits from this u2m pattern; see that README's "Migration note")
- [databricks-builder-app](../databricks-builder-app/) — full-stack apx-agent app demonstrating SP-authenticated `WorkspaceClient` usage
