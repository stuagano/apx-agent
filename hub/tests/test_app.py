import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_ws():
    ws = MagicMock()
    ws.serving_endpoints.list.return_value = []
    ws.api_client.do.return_value = {"spaces": []}
    return ws


@pytest.fixture
def app(mock_ws):
    with patch("hub.app._get_workspace_client", return_value=mock_ws):
        from hub.app import create_hub_app
        return create_hub_app()


@pytest.fixture
def client(app):
    return TestClient(app)


def test_list_agents_empty(client):
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    assert resp.json() == []


def test_register_and_list(client):
    card_data = {
        "name": "test-agent",
        "description": "A test agent",
        "skills": [{"id": "s1", "name": "skill1", "description": "does stuff"}],
    }
    with patch("hub.app.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = card_data
        mock_resp.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = client.post("/api/agents/register", json={"url": "https://test-agent.example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data

    agents = client.get("/api/agents").json()
    assert len(agents) == 1
    assert agents[0]["name"] == "test-agent"
    assert agents[0]["source"] == "apx"


def test_get_agent_not_found(client):
    resp = client.get("/api/agents/nonexistent")
    assert resp.status_code == 404


def test_list_agents_filter_source(client):
    from hub.models import HubAgent
    registry = client.app.state.hub_registry
    registry.add(HubAgent(name="a1", description="d", source="apx"))
    registry.add(HubAgent(name="a2", description="d", source="genie_space"))

    resp = client.get("/api/agents?source=apx")
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["source"] == "apx"


def test_list_agents_filter_query(client):
    from hub.models import HubAgent
    registry = client.app.state.hub_registry
    registry.add(HubAgent(name="billing-agent", description="Handles billing", source="apx"))
    registry.add(HubAgent(name="data-triage", description="Investigates data", source="apx"))

    resp = client.get("/api/agents?q=billing")
    agents = resp.json()
    assert len(agents) == 1
    assert agents[0]["name"] == "billing-agent"


def test_frontend_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
