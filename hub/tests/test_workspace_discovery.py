"""Tests for Hub workspace Apps discovery bootstrap."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_hub.backend.models import AgentCard, AgentTool
from agent_hub.backend.workspace_discovery import bootstrap_workspace_agents
from apx_agent._apps_discovery import AppAgentInfo


@pytest.mark.asyncio
async def test_bootstrap_registers_discovered_apps(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "agent_hub.backend.workspace_discovery.discover_app_agents",
        lambda ws, **k: [
            AppAgentInfo(
                name="data_triage",
                app_name="data-triage-agent",
                url="https://triage.example",
                description="triage",
                tool_count=1,
                state="RUNNING",
                tools=("inspect",),
            )
        ],
    )

    async def crawl(url: str):
        return {
            "name": "data_triage",
            "description": "triage",
            "skills": [{"name": "inspect", "description": "inspect data"}],
        }

    def card_from_a2a(a2a, url, tags=None):
        return AgentCard(
            id="data-triage",
            name=a2a["name"],
            display_name="Data Triage",
            description=a2a.get("description", ""),
            status="live",
            url=url,
            tools=[AgentTool(name="inspect", description="inspect data")],
            tags=tags or [],
            supports_invoke=True,
        )

    agents: dict = {}
    registered = await bootstrap_workspace_agents(
        MagicMock(),
        register_from_a2a=card_from_a2a,
        crawl_agent=crawl,
        agents=agents,
        extra_urls=[],
    )
    assert registered == ["data-triage"]
    assert agents["data-triage"].status == "live"
    assert agents["data-triage"].url == "https://triage.example"


@pytest.mark.asyncio
async def test_bootstrap_overlays_env_urls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "agent_hub.backend.workspace_discovery.discover_app_agents",
        lambda ws, **k: [],
    )

    async def crawl(url: str):
        return {"name": "extra", "description": "", "skills": []}

    def card_from_a2a(a2a, url, tags=None):
        return AgentCard(
            id="extra",
            name="extra",
            display_name="Extra",
            description="",
            status="live",
            url=url,
            tools=[],
            tags=tags or [],
            supports_invoke=True,
        )

    agents: dict = {}
    registered = await bootstrap_workspace_agents(
        MagicMock(),
        register_from_a2a=card_from_a2a,
        crawl_agent=crawl,
        agents=agents,
        extra_urls=["https://extra.example"],
    )
    assert registered == ["extra"]
    assert agents["extra"].tags == ["env"]
