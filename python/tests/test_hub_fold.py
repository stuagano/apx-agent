"""Tests for hub fold into dev-UI (Task 1-8)."""
from __future__ import annotations


def test_hub_models_importable_from_apx_agent():
    """Verify hub card models are importable from apx_agent._hub_models."""
    from apx_agent._hub_models import AgentCard, RegisterRequest, InvokeRequest, AgentTool

    card = AgentCard(
        id="a",
        name="Test Agent",
        display_name="A",
        description="A test agent",
        url="https://x.databricksapps.com",
        status="stub",
        tools=[],
        tags=[],
    )
    assert card.id == "a"
