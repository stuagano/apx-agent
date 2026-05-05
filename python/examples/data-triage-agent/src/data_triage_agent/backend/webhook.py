from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_text(node: object) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = []
        for child in node.get("content", []):
            parts.append(_extract_text(child))
        return " ".join(p for p in parts if p)
    if isinstance(node, list):
        return " ".join(_extract_text(item) for item in node)
    return ""


@router.post("/webhook/jira", status_code=202)
async def jira_webhook(
    request: Request,
    x_hub_signature: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_settings),
) -> dict:
    settings.require_webhook_secret()
    body = await request.body()

    if not x_hub_signature or not _verify_signature(
        body, x_hub_signature, settings.jira_webhook_secret
    ):
        logger.warning("Jira webhook: invalid or missing HMAC signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    fields = issue.get("fields", {})
    issuetype = fields.get("issuetype", {}).get("name", "")

    if event != "jira:issue_created" or issuetype != "Data Issue":
        return JSONResponse(status_code=200, content={"status": "ignored"})

    issue_key = issue.get("key", "")
    summary = fields.get("summary", "")
    description_raw = fields.get("description")
    description = _extract_text(description_raw) if description_raw else ""
    priority = (fields.get("priority") or {}).get("name", "")
    reporter = (fields.get("reporter") or {}).get("displayName", "")

    python_params = ["--issue-key", issue_key, "--summary", summary]
    if description:
        python_params += ["--description", description]
    if priority:
        python_params += ["--priority", priority]
    if reporter:
        python_params += ["--reporter", reporter]

    ws = WorkspaceClient()
    ws.jobs.run_now(job_id=settings.data_triage_job_id, python_params=python_params)
    logger.info("Triggered job %d for ticket %s", settings.data_triage_job_id, issue_key)

    return {"status": "accepted", "issue_key": issue_key}
