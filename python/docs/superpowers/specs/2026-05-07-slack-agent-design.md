# Slack Agent Example — Design Spec

**Date:** 2026-05-07  
**Status:** Approved  
**Location:** `examples/slack-agent/`

## Goal

A Python example that illustrates how apx-agent's OBO (On-Behalf-Of) token system works by building a Slack bot that uses real end-user Databricks credentials. The example makes the "token header magic" explicit: the Slack handler manually does what Databricks Apps does automatically for browser requests.

## The Core Insight

```
Browser → Databricks proxy injects X-Forwarded-Access-Token → /responses → agent runs as real user
Slack   → our handler injects X-Forwarded-Access-Token      → /responses → agent runs as real user
```

`Dependencies.UserClient` always reads `X-Forwarded-Access-Token` from request headers. The Databricks Apps proxy sets it automatically for browser requests. For Slack, we set it ourselves after retrieving the stored user token. Same mechanism, different source.

## OAuth Flow (one-time per Slack user)

1. User types `/connect` in Slack
2. Slack POSTs to `/slack/events`; handler responds immediately with an ephemeral message containing a link to `/slack/install?user=U12345`
3. User clicks → handler redirects to Databricks OIDC:
   ```
   https://{host}/oidc/v1/authorize
     ?response_type=code
     &client_id={client_id}
     &redirect_uri=https://{app_url}/slack/oauth/callback
     &scope=all-apis
     &state=U12345
   ```
4. User authenticates with Databricks
5. Databricks redirects to `/slack/oauth/callback?code=abc&state=U12345`
6. Handler exchanges the code:
   ```
   POST /oidc/v1/token
   grant_type=authorization_code&code=abc&redirect_uri=...&client_id=...&client_secret=...
   ```
7. Handler stores `token_store["U12345"] = access_token`
8. Returns "Connected! Try `/whoami`"

After this, every Slack command from U12345 uses their stored Databricks token.

## Request Flow (after connect)

```
/whoami in Slack
  → POST /slack/events (Slack → app, validated via signing secret)
  → look up token_store["U12345"]
  → if missing: send connect link, return
  → fire async task:
      POST http://localhost:{PORT}/responses  # PORT env var, default 8000
        headers: X-Forwarded-Access-Token: <stored_token>
                 X-Forwarded-Host: <databricks_host>
        body: { "input": [{"role": "user", "content": "/whoami"}] }
  → return 200 to Slack immediately (3-second deadline)
  → async task receives agent response
  → POST to response_url with result
```

The agent's `Dependencies.UserClient` sees `X-Forwarded-Access-Token` in the request headers — exactly as it would from a browser. No changes to the agent code.

## File Structure

```
examples/slack-agent/
├── src/slack_agent/
│   ├── __init__.py
│   └── backend/
│       ├── __init__.py
│       ├── app.py           # create_app(agent) + include slack_router
│       ├── agent_router.py  # LlmAgent with who_am_i tool
│       ├── slack_router.py  # /slack/install, /slack/oauth/callback, /slack/events
│       ├── token_store.py   # dict[str, str] (Option A); Delta table note for Option C
│       └── config.py        # Settings via pydantic-settings
└── pyproject.toml
```

## Components

### `config.py`
Pydantic `BaseSettings` with:
- `databricks_host` — workspace hostname (e.g. `adb-xxx.azuredatabricks.net`)
- `databricks_client_id` / `databricks_client_secret` — OAuth app credentials
- `app_url` — the Databricks App's public URL (used to build `redirect_uri`)
- `slack_signing_secret` — for verifying Slack request signatures
- `slack_bot_token` — for posting messages back (`chat.postMessage`)

### `token_store.py`
Module-level `dict[str, str]` mapping Slack user ID to Databricks access token. Single-process safe. Comment notes:
- Option B extension: swap for `slack_bolt` `InstallationStore`
- Option C extension: swap for Delta table read/write via `WorkspaceClient`

### `agent_router.py`
`LlmAgent` with one tool: `who_am_i(ws: Dependencies.UserClient) -> str` — calls `ws.current_user.me()` and returns the user's display name and email. When called from the browser, `Dependencies.UserClient` reads `X-Forwarded-Access-Token` automatically. When called from Slack (via the self-POST pattern), the Slack handler has injected the stored token into that header — the agent sees no difference.

### `slack_router.py`
Three endpoints:

**`GET /slack/install`** — receives `user` (Slack user ID), builds Databricks OIDC redirect URL, redirects. Stores the Slack user ID in OAuth `state` for the callback.

**`GET /slack/oauth/callback`** — exchanges `code` for access token, stores in `token_store`, returns a plain HTML success page.

**`POST /slack/events`** — validates Slack request signature (HMAC-SHA256, same pattern as the Jira webhook). Parses slash command payload. For `/connect`: returns ephemeral install link. For all other commands: looks up stored token, returns 200 immediately, fires `asyncio.create_task(_dispatch_to_agent(...))`. The dispatch task POSTs to `http://localhost:8000/responses` with the stored token in `X-Forwarded-Access-Token`, then posts the result back to Slack via `response_url`.

### `app.py`
```python
from apx_agent import create_app
from .agent_router import agent
from .slack_router import router as slack_router

app = create_app(agent)
app.include_router(slack_router)
```

## Signature Verification

Same HMAC pattern as the Jira webhook in `data-triage-agent`:
```
HMAC-SHA256(signing_secret, "v0:" + timestamp + ":" + body) == X-Slack-Signature
```
Reject if timestamp is >5 minutes old (replay protection).

## Key Teaching Comments

The moment where the magic becomes visible, in `slack_router.py`:

```python
# Databricks Apps injects X-Forwarded-Access-Token automatically for browser requests.
# Dependencies.UserClient reads it to act as the real user. Here in Slack, we do the
# same thing manually — we fetched the token via OAuth and stored it ourselves.
async with httpx.AsyncClient() as client:
    resp = await client.post(
        f"http://localhost:{os.environ.get('PORT', '8000')}/responses",
        json={"input": [{"role": "user", "content": text}]},
        headers={
            "X-Forwarded-Access-Token": token_store[slack_user_id],
            "X-Forwarded-Host": settings.databricks_host,
        },
    )
```

## What Is Explicitly Out of Scope

- Token refresh (access tokens expire; production needs refresh token rotation)
- Multi-workspace support
- Slack events API (only slash commands via `response_url`)
- Rate limiting / error retries on Slack API calls
- The `who_am_i` tool does anything beyond `current_user.me()`

## Extension Points (commented in code)

- **Option B:** Replace raw FastAPI Slack handling with `slack_bolt.async_app.AsyncApp`
- **Option C:** Replace in-memory `token_store` dict with Delta table via `ws.sql`
