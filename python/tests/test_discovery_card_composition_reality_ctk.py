"""Claim-vs-reality: /.well-known/agent.json reflects the composed agent tree.

The A2A discovery card advertises what the agent can do — its skills, URL,
capabilities. For composed agents (orchestrator + sub-agents, HandoffAgent,
SequentialAgent), the card must correctly aggregate the tool surface.

These tests serve a composed agent via ``create_app`` and assert the card
at ``/.well-known/agent.json`` contains the expected skills and metadata.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")
pytest.importorskip("mlflow")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from apx_agent import (  # noqa: E402
    AgentConfig,
    HandoffAgent,
    LlmAgent,
    SequentialAgent,
    create_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ws() -> MagicMock:
    ws = MagicMock(name="fake_ws")
    ws.config.host = "https://fake.cloud.databricks.com"
    ws.config.authenticate.return_value = {}
    return ws


def _patch_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _fake_ws()
    monkeypatch.setattr("apx_agent._wiring._make_workspace_client", lambda: ws)
    monkeypatch.setattr(
        "apx_agent._defaults._make_workspace_client", lambda **kw: ws
    )
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda *a, **kw: ws)


# Sample tools for card verification
def lookup_invoice(invoice_id: str) -> str:
    """Look up an invoice by ID."""
    return f"Invoice {invoice_id}"


def search_docs(query: str) -> str:
    """Search documentation for an answer."""
    return f"Results for: {query}"


def run_sql(query: str) -> str:
    """Execute a SQL query."""
    return f"SQL: {query}"


# ---------------------------------------------------------------------------
# Orchestrator with local tools + remote sub-agents
# ---------------------------------------------------------------------------


SUB_AGENT_URL = "http://sub-agent.internal"


@pytest.mark.unit
def test_orchestrator_card_includes_local_tools_and_sub_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orchestrator with local tools + config sub_agents lists both."""
    _patch_infra(monkeypatch)

    # Mock the sub-agent card fetch to return a known card
    card_data = {
        "name": "sub-agent",
        "description": "A sub-agent specialist.",
        "url": SUB_AGENT_URL,
        "skills": [
            {"id": "remote_skill", "name": "remote_skill", "description": "A remote skill."}
        ],
    }

    async def mock_get(url, **kwargs):
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        resp.json.return_value = card_data
        resp.raise_for_status = MagicMock()
        return resp

    from unittest.mock import AsyncMock, patch

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(side_effect=mock_get)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = LlmAgent(tools=[lookup_invoice], name="orchestrator")
        config = AgentConfig(
            name="orchestrator",
            description="Routes to specialists.",
            model="model-orch",
            sub_agents=[SUB_AGENT_URL],
        )
        app = create_app(agent, config=config)
        with TestClient(app) as client:
            resp = client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "orchestrator"
    assert card["description"] == "Routes to specialists."

    skill_names = {s["name"] for s in card["skills"]}
    assert "lookup_invoice" in skill_names, f"Local tool missing from card: {skill_names}"
    assert "sub_agent" in skill_names or "remote_skill" in skill_names, (
        f"Sub-agent skill missing from card: {skill_names}"
    )


# ---------------------------------------------------------------------------
# SequentialAgent card aggregates all steps' tools
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sequential_agent_card_aggregates_step_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SequentialAgent's card lists tools from all steps."""
    _patch_infra(monkeypatch)

    step1 = LlmAgent(tools=[lookup_invoice], name="step1")
    step2 = LlmAgent(tools=[search_docs], name="step2")
    step3 = LlmAgent(tools=[run_sql], name="step3")

    pipeline = SequentialAgent(agents=[step1, step2, step3], name="pipeline")
    config = AgentConfig(
        name="data-pipeline",
        description="Multi-step data pipeline.",
        model="model-pipeline",
    )

    app = create_app(pipeline, config=config)
    with TestClient(app) as client:
        resp = client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    card = resp.json()
    skill_names = {s["name"] for s in card["skills"]}
    assert "lookup_invoice" in skill_names, f"Step 1 tool missing: {skill_names}"
    assert "search_docs" in skill_names, f"Step 2 tool missing: {skill_names}"
    assert "run_sql" in skill_names, f"Step 3 tool missing: {skill_names}"


# ---------------------------------------------------------------------------
# HandoffAgent card shows all specialists' tools
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handoff_agent_card_includes_all_specialist_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A HandoffAgent's card lists tools from all constituent agents."""
    _patch_infra(monkeypatch)

    billing = LlmAgent(
        name="billing",
        description="Handle billing.",
        tools=[lookup_invoice],
    )
    support = LlmAgent(
        name="support",
        description="Handle support.",
        tools=[search_docs],
    )
    triage = LlmAgent(
        name="triage",
        description="Route requests.",
        tools=[],
    )

    handoff = HandoffAgent(agents=[triage, billing, support], start="triage")
    config = AgentConfig(
        name="customer-support",
        description="Customer support with handoffs.",
        model="model-cs",
    )

    app = create_app(handoff, config=config)
    with TestClient(app) as client:
        resp = client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "customer-support"
    skill_names = {s["name"] for s in card["skills"]}
    assert "lookup_invoice" in skill_names, f"Billing tool missing: {skill_names}"
    assert "search_docs" in skill_names, f"Support tool missing: {skill_names}"


# ---------------------------------------------------------------------------
# Card capabilities and metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_card_advertises_multi_turn_and_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card's capabilities section correctly advertises multiTurn and streaming."""
    _patch_infra(monkeypatch)

    agent = LlmAgent(tools=[lookup_invoice], name="simple")
    config = AgentConfig(name="simple-agent", description="A simple agent.", model="m")

    app = create_app(agent, config=config)
    with TestClient(app) as client:
        resp = client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    card = resp.json()
    caps = card.get("capabilities", {})
    assert caps.get("multiTurn") is True
    assert caps.get("streaming") is True
