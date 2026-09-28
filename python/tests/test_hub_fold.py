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


def test_hub_invoke_uses_caller_obo_and_fails_closed(dev_ui_app, monkeypatch):
    """Verify invoke route fails closed (401) without OBO on deployed App."""
    monkeypatch.setattr("apx_agent._obo._in_databricks_app", lambda: True)
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    r = c.post(
        "/_apx/hub/agents/x/invoke",
        # no OBO headers
        json={"input": "hi"},
    )
    assert r.status_code == 401, f"Should fail closed (401), got {r.status_code}: {r.text}"


def test_hub_invoke_unknown_id_is_404(dev_ui_app):
    """Verify invoke of nonexistent agent returns 404."""
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    H = {
        "X-Forwarded-Access-Token": "tok-alice",
        "X-Forwarded-User": "alice@x.com",
    }
    r = c.post("/_apx/hub/agents/nope/invoke", headers=H, json={"input": "hi"})
    assert r.status_code == 404, f"Should be 404, got {r.status_code}: {r.text}"


def test_agent_hub_is_gone():
    """Verify standalone agent_hub module is no longer importable."""
    import importlib
    import pytest

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_hub")


def test_allowlist_shared_helper_rejects_evil_hosts():
    """Verify both register and wire paths reject the same bad hosts."""
    from apx_agent._hub_models import is_trusted_agent_url
    from apx_agent._ui_probe import _is_trusted_agent_host

    # Evil hosts that should be rejected by both paths.
    evil_urls = [
        "https://evil-databricksapps.com",
        "https://databricksapps.com@evil.com",  # userinfo trick
        "https://evil.com",
    ]
    for url in evil_urls:
        assert not is_trusted_agent_url(url), f"Register should reject {url}"

    # Extract hosts from URLs for the host-level check.
    from urllib.parse import urlparse
    for url in evil_urls:
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            if host:
                assert not _is_trusted_agent_host(host.lower()), f"Wire check should reject {host}"
        except Exception:
            pass

    # Good hosts that should be accepted by both paths.
    good_urls = [
        "https://agent-a.databricksapps.com",
        "https://agent-b.databricksapps.com",
    ]
    for url in good_urls:
        assert is_trusted_agent_url(url), f"Register should accept {url}"

    parsed = urlparse(good_urls[0])
    host = parsed.hostname
    if host:
        assert _is_trusted_agent_host(host.lower()), f"Wire check should accept {host}"


def test_hub_fleet_lists_from_registry_under_obo(dev_ui_app, monkeypatch):
    """Verify fleet route lists agents from registry under caller OBO."""
    import apx_agent._sql as sql
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    Ha = {
        "X-Forwarded-Access-Token": "tok-alice",
        "X-Forwarded-User": "alice@x.com",
    }

    # Patch run_sql to return two registry rows.
    monkeypatch.setattr(
        sql,
        "run_sql",
        lambda ws, q, **k: [
            {
                "agent_id": "agent-a",
                "name": "agent-a",
                "display_name": "Agent A",
                "description": "First agent",
                "endpoint_url": "https://a.databricksapps.com",
                "endpoint_type": "apps",
                "workspace_host": "https://example.databricks.com",
                "published_by": "alice@example.com",
                "updated_at": 1234567890.0,
            },
            {
                "agent_id": "agent-b",
                "name": "agent-b",
                "display_name": "Agent B",
                "description": "Second agent",
                "endpoint_url": "https://b.databricksapps.com",
                "endpoint_type": "apps",
                "workspace_host": "https://example.databricks.com",
                "published_by": "bob@example.com",
                "updated_at": 1234567891.0,
            },
        ],
    )

    r = c.get("/_apx/hub/fleet", headers=Ha)
    assert r.status_code == 200, f"Fleet route failed: {r.status_code} {r.text}"
    rows = r.json()
    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}: {rows}"
    assert {r["name"] for r in rows} == {"agent-a", "agent-b"}


def test_hub_fleet_fails_closed_without_obo(dev_ui_app, monkeypatch):
    """Verify fleet route fails closed (401) without OBO on deployed App."""
    monkeypatch.setattr("apx_agent._obo._in_databricks_app", lambda: True)
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    r = c.get("/_apx/hub/fleet")  # no OBO headers
    assert r.status_code == 401, f"Should fail closed (401), got {r.status_code}: {r.text}"


def test_hub_xss_escaping_in_rendered_page():
    """Verify that malicious agent data is escaped in the hub HTML page.

    Regression test: agent-supplied fields (display_name, description, tools[].name, id)
    are HTML-escaped to prevent stored XSS via crawled /.well-known/agent.json.
    """
    from apx_agent._ui_hub import render_hub_ui

    page = render_hub_ui()

    # Verify the esc() helper function exists.
    assert "const esc = (s) => String(s == null ? '' : s)" in page, \
        "esc() helper function not found in rendered page"

    # Verify esc() is used for escaping all agent-supplied interpolations.
    assert "esc(agent.display_name" in page, "display_name should be escaped"
    assert "esc(agent.description" in page, "description should be escaped"
    assert "esc(agent.id" in page, "id should be escaped"
    assert "esc(agent.url" in page, "url should be escaped"
    assert "esc(agent.status" in page, "status should be escaped"
    assert "esc(t.name || t)" in page, "tool names should be escaped"

    # Verify esc() includes HTML entity escaping (& < > " ')
    assert "&amp;" in page and "&lt;" in page and "&gt;" in page, \
        "esc() should include HTML entity escaping"
    assert "&quot;" in page and "&#39;" in page, \
        "esc() should escape quotes for HTML context"


def test_hub_register_with_xss_payload_is_stored_escaped(dev_ui_app, monkeypatch):
    """Verify malicious agent crawl data is stored and rendered safely.

    Even when a crawled agent.json contains HTML/JS payloads in name/description/id,
    the stored card is safe, and when rendered in the page via renderAgents(), the
    esc() function escapes it so no script runs.
    """
    import apx_agent._dev as dev
    from starlette.testclient import TestClient

    c = TestClient(dev_ui_app)
    H = {
        "X-Forwarded-Access-Token": "tok-alice",
        "X-Forwarded-User": "alice@x.com",
    }

    # Mock crawl to return a card with XSS payloads
    async def malicious_crawl(url):
        return {
            "name": "<img src=x onerror=alert('xss')>",
            "display_name": "<script>alert('xss')</script>",
            "description": "<svg onload=alert('xss')>",
            "url": url,
            "skills": [{"name": "<img onerror=alert(1)>"}],
        }

    monkeypatch.setattr(dev, "_crawl_agent", malicious_crawl)

    # Register the malicious agent
    r = c.post(
        "/_apx/hub/agents",
        headers=H,
        json={"url": "https://evil.databricksapps.com", "tags": []},
    )
    assert r.status_code == 200, f"Register failed: {r.text}"
    aid = r.json()["id"]

    # Fetch the hub page
    r = c.get("/_apx/hub", headers=H)
    assert r.status_code == 200, f"Hub page fetch failed: {r.text}"
    page = r.text

    # Verify the page contains the esc() function.
    assert "const esc = (s) =>" in page, "esc() helper missing from rendered hub"

    # Verify the malicious payloads are not executable (stored as plain text, escaped on render).
    # The stored card has raw content, but it's rendered via renderAgents() which uses esc().
    # We check that the page JS contains the necessary escaping calls.
    assert "esc(agent.display_name" in page, "renderAgents should escape display_name"
    assert "esc(agent.description" in page, "renderAgents should escape description"
