# slack-agent

> **Direction: Slack → Databricks**
>
> This example solves: *"A Slack user wants to query Databricks."*
> Auth flows **into** Databricks — the Slack user authenticates via Databricks OIDC and the agent runs with their Databricks identity.
>
> This is **not** the pattern for giving Genie read-access to Slack. For that direction
> (Databricks → Slack, respecting per-user Slack permissions), use a UC External Connection
> pointing at `https://mcp.slack.com/mcp` with per-user OAuth2 — no custom agent needed.

A Slack bot that runs apx-agent with real end-user Databricks credentials. The purpose of this example is to make the `X-Forwarded-Access-Token` mechanism explicit — the same token-injection that Databricks Apps does automatically for browser requests, done manually for Slack.

## The Core Idea

```
Browser → Databricks Apps proxy injects X-Forwarded-Access-Token → /responses → agent runs as real user
Slack   → this handler injects X-Forwarded-Access-Token          → /responses → agent runs as real user
```

`Dependencies.UserClient` always reads `X-Forwarded-Access-Token` from request headers. For browser requests, the Databricks Apps proxy sets it automatically. For Slack, we set it ourselves after retrieving the stored user token. Same mechanism, different source — the agent code sees no difference.

The moment where this becomes visible, in `slack_router.py`:

```python
# Databricks Apps injects X-Forwarded-Access-Token automatically for browser requests.
# Dependencies.UserClient reads it to act as the real user. Here in Slack, we do the
# same thing manually — we fetched the token via OAuth and stored it ourselves.
await client.post(
    f"http://localhost:{port}/responses",
    json={"input": [{"role": "user", "content": text}]},
    headers={
        "X-Forwarded-Access-Token": databricks_token,
        "X-Forwarded-Host": databricks_host,
    },
)
```

## OAuth Flow (one-time per Slack user)

1. User types `/connect` in Slack
2. Handler returns an ephemeral message with a link to `/slack/install?user=U12345`
3. User clicks → redirected to Databricks OIDC (`/oidc/v1/authorize`)
4. User authenticates → Databricks redirects to `/slack/oauth/callback?code=...&state=<nonce>`
5. Handler exchanges code for access token, stores it keyed by Slack user ID
6. Returns "Connected!" — every subsequent command uses the stored token

The OAuth `state` parameter is a server-side nonce (not the raw Slack user ID) to prevent CSRF. The nonce is stored in `_pending: dict[str, str]` and popped on callback.

## Request Flow (after connect)

```
/whoami in Slack
  → POST /slack/events (validated via HMAC-SHA256 signing secret)
  → look up stored token for user
  → return 200 immediately (Slack's 3-second deadline)
  → background task: POST /responses with X-Forwarded-Access-Token
  → agent calls who_am_i tool → ws.current_user.me()
  → POST result back to Slack via response_url
```

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Slack workspace | Admin access to create a Slack app at api.slack.com |
| Databricks workspace | Apps enabled, OAuth app configured (see Setup) |

```bash
git clone https://github.com/stuagano/apx-agent
cd apx-agent/python/examples/slack-agent
uv sync
```

## Part 1: Workspace setup (one-time)

### Step 1: Create a Databricks OAuth app

In your Databricks workspace: **Settings → Developer → App Connections → Create app**

- Name: anything (e.g. `slack-agent`)
- Redirect URI: `https://{your-app-url}/slack/oauth/callback`
  - If running locally with ngrok: `https://{ngrok-subdomain}.ngrok.io/slack/oauth/callback`
- Save the **Client ID** and **Client Secret**

### Step 2: Create a Slack app

At [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From Scratch**

**Add a slash command** (under *Slash Commands*):
- Command: `/connect`
- Request URL: `https://{your-app-url}/slack/events`
- Repeat for any other commands you want (e.g. `/whoami`)

**Enable OAuth** (under *OAuth & Permissions*):
- Add redirect URL: `https://{your-app-url}/slack/oauth/callback`
- Add bot token scopes: `commands`, `chat:write`

Install the app to your workspace and grab:
- **Signing Secret** (under *Basic Information → App Credentials*)
- **Bot Token** (`xoxb-...`) (under *OAuth & Permissions*)

---

## Part 2: Local development

### Step 1: Configure environment

Copy `.env.example` to `.env` and fill in:

```
DATABRICKS_HOST=adb-xxx.azuredatabricks.net
DATABRICKS_CLIENT_ID=...
DATABRICKS_CLIENT_SECRET=...
APP_URL=https://your-app.databricksapps.com
SLACK_SIGNING_SECRET=...
SLACK_BOT_TOKEN=xoxb-...
```

`APP_URL` must be the public URL that Slack can reach — either your Databricks Apps URL or an ngrok tunnel for local development.

### Step 2: Run locally

```bash
uv sync
uv run uvicorn slack_agent.backend.app:app --reload
```

### Step 3: Connect your Databricks account

In Slack, type `/connect`. Click the link in the ephemeral response, authenticate with Databricks, and you'll be redirected back with "Connected!". Then try `/whoami`.

### Step 4: Run tests

```bash
uv run pytest tests/ -v   # 25 passed
```

---

## Part 3: Deploy to Databricks Apps

### Step 1: Set `APP_URL` in `.env`

Change `APP_URL` from your ngrok tunnel (or `localhost`) to your Databricks Apps public URL:

```
APP_URL=https://your-app.databricksapps.com
```

### Step 2: Update redirect URIs

- **Databricks OAuth app** — update the redirect URI to `https://{your-app-url}/slack/oauth/callback`
- **Slack app** — update the slash command Request URL to `https://{your-app-url}/slack/events` and the OAuth redirect URL to `https://{your-app-url}/slack/oauth/callback`

### Step 3: Deploy

```bash
uv run apx deploy
```

### Step 4: Verify

```bash
databricks apps get slack-agent
# look for "state": "RUNNING"
```

## File Structure

```
src/slack_agent/backend/
├── app.py           # create_app(agent) + include slack_router
├── agent_router.py  # LlmAgent with who_am_i tool
├── slack_router.py  # /slack/install, /slack/oauth/callback, /slack/events
├── token_store.py   # dict[str, str] keyed by Slack user ID
└── config.py        # pydantic-settings BaseSettings
```

## Testing

25 unit tests, zero live dependencies. The key pattern that makes this possible:

**Extract async dispatch as a plain function with explicit primitive args.**

```python
async def _dispatch_to_agent(
    text: str,
    slack_user_id: str,
    response_url: str,
    databricks_token: str,   # ← the OBO token, as a plain string
    databricks_host: str,
) -> None:
    ...
```

Because `_dispatch_to_agent` accepts primitive strings (not injected objects), tests can mock it directly and assert the exact token being forwarded:

```python
with patch("slack_agent.backend.slack_router._dispatch_to_agent") as mock_dispatch:
    client.post("/slack/events", data=payload, headers=slack_headers)
    mock_dispatch.assert_called_once_with(
        text="hello",
        slack_user_id="U123",
        response_url="https://hooks.slack.com/resp/abc",
        databricks_token="dapi-real-token",   # ← exact OBO token verified
        databricks_host="adb-123.azuredatabricks.net",
    )
```

This pattern — extract dispatch, pass primitives, mock directly — is reusable for any apx-agent example with a webhook or background task.

```bash
uv run pytest tests/ -v   # 25 passed
```

## Extension Points

- **Option B:** Replace raw FastAPI Slack handling with `slack_bolt.async_app.AsyncApp`
- **Option C:** Replace in-memory `token_store` dict with Delta table via `ws.sql`

## Out of Scope

- Token refresh (access tokens expire; production needs refresh token rotation)
- Slack Events API (this example uses slash commands + `response_url` only)
- Rate limiting / retry logic on Slack API calls
