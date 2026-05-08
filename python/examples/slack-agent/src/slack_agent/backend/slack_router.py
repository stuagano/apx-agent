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
