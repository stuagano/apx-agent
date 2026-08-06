"""#635: empty routing descriptions must never reach a tool schema.

``agent_tool`` already falls back when ``description`` is empty. Two residual
paths did not:

  * ``RouterAgent`` explicit ``(name, description, agent)`` triples — an empty
    string passed straight through into ``transfer_to_<name>`` schemas.
  * A remote A2A card advertising ``"description": ""`` — the empty string
    became the sub-agent tool's description.

Either way the routing LLM is asked to choose a tool with no stated purpose.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import LlmAgent, RouterAgent

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _noop_tool(query: str) -> str:
    """Echo the query."""
    return query


# ---------------------------------------------------------------------------
# RouterAgent explicit tuple form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_router_tuple_blank_description_is_normalized(blank: str) -> None:
    """#635: a blank triple description becomes the generic routing string."""
    router = RouterAgent([("billing", blank, LlmAgent(tools=[_noop_tool]))])

    name, description, _ = router._routes[0]
    assert name == "billing"
    assert description == "Routes to the billing agent."


def test_router_tuple_description_is_preserved_when_present() -> None:
    router = RouterAgent(
        [("billing", "Handles invoices and refunds.", LlmAgent(tools=[_noop_tool]))]
    )
    assert router._routes[0][1] == "Handles invoices and refunds."


def test_router_transfer_tool_schemas_never_carry_empty_descriptions() -> None:
    """The LLM-visible schema is the surface that matters."""
    router = RouterAgent(
        [
            ("billing", "", LlmAgent(tools=[_noop_tool])),
            ("tech", "Debugs errors.", LlmAgent(tools=[_noop_tool])),
        ]
    )
    for schema in router._transfer_tool_schemas():
        assert schema["function"]["description"].strip(), (
            f"empty description reached tool schema: {schema} (#635)"
        )


# ---------------------------------------------------------------------------
# Remote A2A card with an empty description
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_card_empty_description_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#635: a card advertising description="" must not describe the tool."""
    url = "https://peer.aws.databricksapps.com"
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: MagicMock())

    async def fake_card(
        self: Any, client: Any, base_url: str, auth_headers: dict[str, str]
    ) -> dict[str, Any]:
        return {"name": "peer-agent", "description": "", "skills": []}

    monkeypatch.setattr(
        "apx_agent._agents.LlmAgent._fetch_sub_agent_card", fake_card
    )

    agent = LlmAgent(tools=[_noop_tool], instructions="Orchestrate.")
    agent._sub_agent_urls = [url]

    (descriptor,) = await agent.fetch_remote_tools()
    assert descriptor.description.strip(), (
        f"empty card description reached the descriptor: {descriptor!r} (#635)"
    )
    assert descriptor.description == f"Agent at {url}"


@pytest.mark.asyncio
async def test_remote_card_description_is_preserved_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://peer.aws.databricksapps.com"
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: MagicMock())

    async def fake_card(
        self: Any, client: Any, base_url: str, auth_headers: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "name": "peer-agent",
            "description": "Answers billing questions.",
            "skills": [],
        }

    monkeypatch.setattr(
        "apx_agent._agents.LlmAgent._fetch_sub_agent_card", fake_card
    )

    agent = LlmAgent(tools=[_noop_tool], instructions="Orchestrate.")
    agent._sub_agent_urls = [url]

    (descriptor,) = await agent.fetch_remote_tools()
    assert descriptor.description == "Answers billing questions."
