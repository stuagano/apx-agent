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


def test_hub_store_is_per_principal():
    """Verify HubStore keeps registrations per principal."""
    from apx_agent._hub_store import HubStore
    from apx_agent._hub_models import AgentCard

    s = HubStore()
    card = AgentCard(
        id="x",
        name="Agent X",
        display_name="X",
        description="Agent X",
        url="https://x.databricksapps.com",
        status="live",
        tools=[],
        tags=[],
    )
    s.put("alice", card)
    assert [c.id for c in s.list("alice")] == ["x"]
    assert s.list("bob") == []  # bob cannot see alice's agent
    assert s.get("bob", "x") is None
    assert s.delete("bob", "x") is False  # bob cannot delete alice's agent
    assert s.delete("alice", "x") is True
