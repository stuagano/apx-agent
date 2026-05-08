from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import Settings, get_settings
from . import token_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/slack")


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

    Passes the Slack user ID as OAuth 'state' so the callback can store
    the resulting token against the correct Slack user.
    """
    params = urlencode({
        "response_type": "code",
        "client_id": settings.databricks_client_id,
        "redirect_uri": f"{settings.app_url}/slack/oauth/callback",
        "scope": "all-apis",
        "state": user,
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

    slack_user_id = state
    token_store.set_token(slack_user_id, access_token)
    logger.info("Stored Databricks token for Slack user %s", slack_user_id)

    return HTMLResponse(
        content=(
            "<h1>Connected!</h1>"
            "<p>Your Databricks account is linked. Try <code>/whoami</code> in Slack.</p>"
        ),
        status_code=200,
    )
