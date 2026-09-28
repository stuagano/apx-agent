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


def test_hub_refresh_updates_status_for_caller(dev_ui_app, monkeypatch):
    """Verify refresh route re-crawls and updates status (live/unreachable)."""
    import apx_agent._dev as dev
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    H = lambda who: {
        "X-Forwarded-Access-Token": f"tok-{who}",
        "X-Forwarded-User": f"{who}@x.com",
    }

    # 1. crawl stub returns a live card → register succeeds
    async def live(url):
        return {"name": "A", "url": url, "skills": []}

    monkeypatch.setattr(dev, "_crawl_agent", live)
    r = c.post(
        "/_apx/hub/agents",
        headers=H("alice"),
        json={"url": "https://a.databricksapps.com", "tags": []},
    )
    assert r.status_code == 200, f"Register failed: {r.text}"
    aid = r.json()["id"]
    initial_card = r.json()
    assert initial_card["status"] == "live", f"Initial status should be live, got {initial_card}"

    # 2. flip the stub to unreachable → refresh marks it unreachable
    async def dead(url):
        return None

    monkeypatch.setattr(dev, "_crawl_agent", dead)
    r = c.post(f"/_apx/hub/agents/{aid}/refresh", headers=H("alice"))
    assert r.status_code == 200, f"Refresh failed: {r.text}"
    refreshed = r.json()
    assert refreshed["status"] == "unreachable", f"Status should be unreachable, got {refreshed}"
    assert refreshed["last_seen"] is None, f"last_seen should be None on unreachable, got {refreshed}"


def test_hub_refresh_unknown_id_is_404(dev_ui_app):
    """Verify refresh of nonexistent agent returns 404."""
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    H = {
        "X-Forwarded-Access-Token": "tok-alice",
        "X-Forwarded-User": "alice@x.com",
    }
    r = c.post("/_apx/hub/agents/nope/refresh", headers=H)
    assert r.status_code == 404, f"Should be 404, got {r.status_code}: {r.text}"
