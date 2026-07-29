"""Workspace discovery bootstrap for Agent Hub.

On startup (and via ``POST /api/agents/discover-workspace``), list Databricks
Apps, probe each ``/.well-known/agent.json``, and register live apx agents.
``AGENT_HUB_AGENT_URLS`` remains an overlay for agents that aren't Apps or
aren't listable yet.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from apx_agent._apps_discovery import discover_app_agents

logger = logging.getLogger(__name__)


async def bootstrap_workspace_agents(
    ws: Any,
    *,
    register_from_a2a,
    crawl_agent,
    agents: dict[str, Any],
    extra_urls: list[str] | None = None,
    skip_ids: set[str] | None = None,
) -> list[str]:
    """Discover + register workspace Apps agents; overlay ``extra_urls``.

    Returns the list of agent ids that were registered or refreshed as live.
    ``register_from_a2a`` is ``_card_from_a2a``; ``crawl_agent`` is
    ``_crawl_agent``. Stub/example seeds in ``agents`` are left alone unless
    a discovered agent shares their id.
    """
    registered: list[str] = []
    skip = skip_ids or set()

    try:
        found = await asyncio.to_thread(discover_app_agents, ws)
    except Exception as e:
        logger.warning("Workspace Apps discovery failed: %s", e)
        found = []

    for info in found:
        try:
            a2a = await crawl_agent(info.url)
            if not a2a:
                # Card probe already succeeded in discover_app_agents — synthesize
                # a minimal A2A doc so registration still works if httpx crawl fails.
                a2a = {
                    "name": info.name,
                    "description": info.description or "",
                    "skills": [{"name": t, "description": ""} for t in info.tools],
                }
            card = register_from_a2a(a2a, info.url, tags=["workspace"])
            if card.id in skip:
                continue
            agents[card.id] = card
            registered.append(card.id)
            logger.info(
                "Workspace-discovered '%s' from %s (%d tools)",
                card.id, info.url, len(card.tools),
            )
        except Exception as e:
            logger.warning("Failed to register discovered app %s: %s", info.app_name, e)

    for url in extra_urls or []:
        try:
            a2a = await crawl_agent(url)
            if not a2a:
                logger.warning("Could not reach %s — skipping", url)
                continue
            card = register_from_a2a(a2a, url, tags=["env"])
            agents[card.id] = card
            registered.append(card.id)
            logger.info(
                "Auto-registered '%s' from %s (%d tools)",
                card.id, url, len(card.tools),
            )
        except Exception as e:
            logger.warning("Auto-register failed for %s: %s", url, e)

    return registered
