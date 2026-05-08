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
