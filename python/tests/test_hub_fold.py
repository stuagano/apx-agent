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


def test_hub_register_and_list_is_caller_scoped(dev_ui_app, monkeypatch):
    """Verify hub register/list/get/deregister routes are caller-scoped."""
    import apx_agent._dev as dev
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    H = lambda who: {
        "X-Forwarded-Access-Token": f"tok-{who}",
        "X-Forwarded-User": f"{who}@x.com",
    }

    # Mock the A2A crawl to return a stub card
    async def mock_crawl(url):
        return {"name": "test_agent", "description": "Test agent", "skills": []}

    monkeypatch.setattr(dev, "_crawl_agent", mock_crawl)

    # Alice registers an agent
    r = c.post(
        "/_apx/hub/agents",
        headers=H("alice"),
        json={"url": "https://a.databricksapps.com", "tags": []},
    )
    assert r.status_code == 200, f"Register failed: {r.text}"
    aid = r.json()["id"]

    # Alice can see her agent
    agents_alice = c.get("/_apx/hub/agents", headers=H("alice")).json()
    assert any(a["id"] == aid for a in agents_alice), f"Alice should see her agent, got {agents_alice}"

    # Bob cannot see Alice's agent
    agents_bob = c.get("/_apx/hub/agents", headers=H("bob")).json()
    assert agents_bob == [], f"Bob should not see Alice's agent, got {agents_bob}"

    # Bob cannot delete Alice's agent (404)
    r = c.delete(f"/_apx/hub/agents/{aid}", headers=H("bob"))
    assert r.status_code == 404, f"Bob should get 404, got {r.status_code}: {r.text}"


def test_hub_register_fails_closed_without_obo(dev_ui_app, monkeypatch):
    """Verify register route fails closed (401) without OBO on deployed App."""
    monkeypatch.setattr("apx_agent._obo._in_databricks_app", lambda: True)
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    r = c.post(
        "/_apx/hub/agents",
        # no X-Forwarded-* headers
        json={"url": "https://a.databricksapps.com", "tags": []},
    )
    assert r.status_code == 401, f"Should fail closed, got {r.status_code}: {r.text}"
