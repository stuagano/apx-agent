from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings, get_settings
from . import token_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack")

# Short-lived nonce store: nonce → slack_user_id.
# In production, add TTL expiry. For this example, in-memory is fine.
_pending: dict[str, str] = {}


def _verify_slack_signature(body: bytes, timestamp: str, signature: str, secret: str) -> bool:
    """Validate Slack's HMAC-SHA256 request signature.

    Slack signs requests with: HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
    Rejects requests with timestamps older than 5 minutes (replay protection).
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts) > 300:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(secret.encode(), basestring.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.get("/install")
async def install(
    user: str = Query(..., description="Slack user ID to associate with the Databricks token"),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Redirect to the Databricks OIDC authorization URL.

    Uses a server-side nonce as OAuth 'state' (not the raw Slack user ID)
    so the callback can verify the request originated from this app's redirect.
    """
    nonce = secrets.token_urlsafe(16)
    _pending[nonce] = user
    params = urlencode({
        "response_type": "code",
        "client_id": settings.databricks_client_id,
        "redirect_uri": f"{settings.app_url}/slack/oauth/callback",
        "scope": "all-apis",
        "state": nonce,
    })
    return RedirectResponse(
        url=f"https://{settings.databricks_host}/oidc/v1/authorize?{params}"
    )


@router.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(..., description="Slack user ID passed through OAuth state"),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Exchange the Databricks authorization code for an access token.

    This is the manual version of what Databricks Apps does automatically for
    browser requests. The Apps proxy injects X-Forwarded-Access-Token so that
    Dependencies.UserClient can read it. Here, we fetch the token ourselves via
    OAuth and store it — then inject it the same way in _dispatch_to_agent().
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://{settings.databricks_host}/oidc/v1/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{settings.app_url}/slack/oauth/callback",
                "client_id": settings.databricks_client_id,
                "client_secret": settings.databricks_client_secret,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {resp.text}")

    access_token = resp.json().get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token in Databricks response")

    slack_user_id = _pending.pop(state, None)
    if slack_user_id is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    token_store.set_token(slack_user_id, access_token)
    logger.info("Stored Databricks token for Slack user %s", slack_user_id)

    return HTMLResponse(
        content=(
            "<h1>Connected!</h1>"
            "<p>Your Databricks account is linked. Try <code>/whoami</code> in Slack.</p>"
        ),
        status_code=200,
    )


@router.post("/events")
async def slack_events(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """Handle Slack slash commands.

    Validates Slack's HMAC-SHA256 signature, then:
    - /connect: returns an ephemeral message with the OAuth install link.
    - anything else: looks up the stored Databricks token; if found, returns
      200 immediately and fires an async task that runs the agent and posts
      the result back to Slack via response_url (3-second deadline workaround).
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature, settings.slack_signing_secret):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    user_id = str(form.get("user_id", ""))
    text = str(form.get("text", "")).strip()
    response_url = str(form.get("response_url", ""))
    command = str(form.get("command", ""))

    if command == "/connect":
        install_url = f"{settings.app_url}/slack/install?user={user_id}"
        return {
            "response_type": "ephemeral",
            "text": f"Click to connect your Databricks account: {install_url}",
        }

    stored_token = token_store.get_token(user_id)
    if not stored_token:
        install_url = f"{settings.app_url}/slack/install?user={user_id}"
        return {
            "response_type": "ephemeral",
            "text": f"Connect your Databricks account first: {install_url}",
        }

    # Slack requires a response within 3 seconds. Return immediately and do
    # the agent work in the background, posting back via response_url.
    asyncio.create_task(
        _dispatch_to_agent(
            text=text or command,
            slack_user_id=user_id,
            response_url=response_url,
            databricks_token=stored_token,
            databricks_host=settings.databricks_host,
        )
    )
    return {"response_type": "ephemeral", "text": "Working on it..."}


async def _dispatch_to_agent(
    text: str,
    slack_user_id: str,
    response_url: str,
    databricks_token: str,
    databricks_host: str,
) -> None:
    """Call the agent and post the result back to Slack via response_url.

    Databricks Apps injects X-Forwarded-Access-Token automatically for browser
    requests. Dependencies.UserClient reads it to create a WorkspaceClient for
    the real user. Here in Slack, we do the same thing manually — we fetched
    the token via Databricks OAuth and stored it; now we inject it into the
    request headers so the agent sees no difference.
    """
    port = os.environ.get("PORT", "8000")
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            agent_resp = await client.post(
                f"http://localhost:{port}/responses",
                json={"input": [{"role": "user", "content": text}]},
                headers={
                    "X-Forwarded-Access-Token": databricks_token,
                    "X-Forwarded-Host": databricks_host,
                },
            )
            agent_resp.raise_for_status()
            result_text = agent_resp.json().get("output_text", "(no response)")

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(response_url, json={"text": result_text})

    except Exception:
        logger.exception("Error dispatching to agent for Slack user %s", slack_user_id)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(response_url, json={"text": "Sorry, something went wrong."})
        except Exception:
            logger.exception("Error posting error response to Slack for user %s", slack_user_id)
