"""#636: never advertise a sub-agent whose callable was not registered.

``fetch_remote_tools`` appended the card descriptor and *then* registered the
callable delegate. On a tool-name collision, ``_ensure_sub_agent_tool`` keeps
the existing local implementation — so the A2A card advertised delegation to a
peer while calls to that name hit the unrelated local tool. Advertised and
callable must be the same thing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import LlmAgent

PEER_URL = "https://peer.aws.databricksapps.com"


def peer_agent(query: str) -> str:
    """Local tool whose name collides with the peer's card name."""
    return f"local:{query}"


def unrelated_tool(query: str) -> str:
    """Local tool with no name collision."""
    return f"local:{query}"


@pytest.fixture
def stub_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: MagicMock())

    async def fake_card(
        self: Any, client: Any, base_url: str, auth_headers: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "name": "peer-agent",
            "description": "Answers billing questions.",
            "skills": [],
        }

    monkeypatch.setattr("apx_agent._agents.LlmAgent._fetch_sub_agent_card", fake_card)


@pytest.mark.asyncio
async def test_colliding_sub_agent_is_not_advertised(stub_card: None) -> None:
    """#636: the colliding name must be absent from the advertised descriptors."""
    agent = LlmAgent(tools=[peer_agent], instructions="Orchestrate.")
    agent._sub_agent_urls = [PEER_URL]

    tools = await agent.fetch_remote_tools()

    assert [t.name for t in tools] == [], (
        "advertised a sub-agent whose callable was never registered (#636)"
    )


@pytest.mark.asyncio
async def test_colliding_name_keeps_the_local_implementation(stub_card: None) -> None:
    """The existing local tool stays intact — no silent replacement."""
    agent = LlmAgent(tools=[peer_agent], instructions="Orchestrate.")
    agent._sub_agent_urls = [PEER_URL]

    await agent.fetch_remote_tools()

    matching = [fn for fn in agent._tool_fns if fn.__name__ == "peer_agent"]
    assert len(matching) == 1
    assert matching[0] is peer_agent
    assert not hasattr(matching[0], "__apx_sub_agent_url__")


@pytest.mark.asyncio
async def test_collision_is_logged_as_not_advertised(
    stub_card: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning must say the sub-agent is unavailable, not 'advertised'."""
    agent = LlmAgent(tools=[peer_agent], instructions="Orchestrate.")
    agent._sub_agent_urls = [PEER_URL]

    with caplog.at_level("WARNING"):
        await agent.fetch_remote_tools()

    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "collides" in warning
    assert "not advertised" in warning


@pytest.mark.asyncio
async def test_non_colliding_sub_agent_is_still_advertised(stub_card: None) -> None:
    """Guard against over-correction: the normal path must keep working."""
    agent = LlmAgent(tools=[unrelated_tool], instructions="Orchestrate.")
    agent._sub_agent_urls = [PEER_URL]

    tools = await agent.fetch_remote_tools()

    assert [t.name for t in tools] == ["peer_agent"]
    assert tools[0].sub_agent_url == PEER_URL
    # Advertised implies callable: the delegate is registered under that name.
    delegate = [fn for fn in agent._tool_fns if fn.__name__ == "peer_agent"]
    assert len(delegate) == 1
