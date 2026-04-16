"""Discover agents from workspace serving endpoints and Genie spaces."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .models import HubAgent

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
