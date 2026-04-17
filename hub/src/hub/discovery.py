"""Discover agents from workspace serving endpoints, Genie spaces, and Databricks Apps."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from .models import HubAgent, HubSkill

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


def discover_serving_endpoints(ws: WorkspaceClient) -> list[HubAgent]:
    """List serving endpoints that look like agents (chat task or agent tag)."""
    agents: list[HubAgent] = []
    try:
        endpoints = ws.serving_endpoints.list()
    except Exception as e:
        logger.warning("Failed to list serving endpoints: %s", e)
        return agents

    for ep in endpoints:
        task = getattr(ep, "task", None) or ""
        tags = getattr(ep, "tags", None) or {}
        is_chat = task == "llm/v1/chat"
        is_tagged = tags.get("agent") is not None

        if not is_chat and not is_tagged:
            continue

        state = getattr(getattr(ep, "state", None), "ready", "NOT_READY")
        agents.append(HubAgent(
            name=ep.name,
            description=f"Serving endpoint: {ep.name}",
            source="serving_endpoint",
            url=ep.name,
            status="online" if state == "READY" else "offline",
            metadata={"task": task, "tags": tags},
        ))

    logger.info("Discovered %d serving endpoints", len(agents))
    return agents


def discover_genie_spaces(ws: WorkspaceClient) -> list[HubAgent]:
    """List Genie spaces from the workspace."""
    agents: list[HubAgent] = []
    try:
        response = ws.api_client.do("GET", "/api/2.0/genie/spaces")
    except Exception as e:
        logger.warning("Failed to list Genie spaces: %s", e)
        return agents

    spaces = response.get("spaces", []) if isinstance(response, dict) else []
    for space in spaces:
        space_id = space.get("space_id", "")
        title = space.get("title", space_id)
        description = space.get("description", f"Genie space: {title}")

        agents.append(HubAgent(
            name=title,
            description=description,
            source="genie_space",
            status="online",
            metadata={"space_id": space_id},
        ))

    logger.info("Discovered %d Genie spaces", len(agents))
    return agents


async def discover_apps(ws: "WorkspaceClient", self_url: str | None = None, obo_token: str | None = None) -> list[HubAgent]:
    """Probe all Databricks Apps for /.well-known/agent.json and return apx agents found."""
    agents: list[HubAgent] = []

    try:
        all_apps = list(ws.apps.list())
    except Exception as e:
        logger.warning("Failed to list Databricks Apps: %s", e)
        return agents

    # Prefer user OBO token (works for cross-app calls on Databricks Apps);
    # fall back to SP M2M credentials for local/non-Apps environments.
    if obo_token:
        auth_headers = {"Authorization": f"Bearer {obo_token}"}
    else:
        try:
            auth_headers = ws.config.authenticate()
        except Exception:
            auth_headers = {}

    logger.info("Probing %d Databricks Apps for agent cards", len(all_apps))
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        for app in all_apps:
            url = getattr(app, "url", None)
            if not url:
                continue
            # Skip the hub itself
            if self_url and url.rstrip("/") == self_url.rstrip("/"):
                continue

            card_url = f"{url.rstrip('/')}/.well-known/agent.json"
            try:
                resp = await client.get(card_url, headers=auth_headers)
                logger.info("  %s → %d", app.name, resp.status_code)
                if resp.status_code != 200:
                    continue
                card = resp.json()
            except Exception as e:
                logger.info("  %s → error: %s", app.name, e)
                continue

            skills = [
                HubSkill(name=s.get("name", ""), description=s.get("description", ""))
                for s in card.get("skills", [])
            ]
            agents.append(HubAgent(
                name=card.get("name", app.name),
                description=card.get("description", ""),
                source="apx",
                url=url.rstrip("/"),
                skills=skills,
                status="online",
            ))
            logger.info("Discovered apx agent: %s (%s)", card.get("name", app.name), url)

    logger.info("Discovered %d apx agents from Databricks Apps", len(agents))
    return agents
