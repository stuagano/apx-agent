import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from slack_agent.backend.app import app
from slack_agent.backend.config import Settings, get_settings
from slack_agent.backend import token_store

DATABRICKS_HOST = "adb-123.azuredatabricks.net"
APP_URL = "https://my-app.databricksapps.com"


def _make_settings():
    return Settings(
        databricks_host=DATABRICKS_HOST,
        databricks_client_id="my-client-id",
        databricks_client_secret="my-client-secret",
        app_url=APP_URL,
        slack_signing_secret="signing-secret",
        slack_bot_token="xoxb-bot-token",
    )


@pytest.fixture(autouse=True)
def clear_store():
    token_store._store.clear()
    yield
    token_store._store.clear()


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_install_redirects_to_databricks_oidc(client):
    resp = client.get("/slack/install?user=U123", follow_redirects=False)
    assert resp.status_code in (302, 307)
    location = resp.headers["location"]
    assert f"https://{DATABRICKS_HOST}/oidc/v1/authorize" in location
    assert "client_id=my-client-id" in location
    assert "state=U123" in location
    assert "response_type=code" in location
    assert "scope=all-apis" in location


def test_install_includes_redirect_uri(client):
    resp = client.get("/slack/install?user=U123", follow_redirects=False)
    location = resp.headers["location"]
    assert "redirect_uri=" in location
    assert "slack%2Foauth%2Fcallback" in location or "slack/oauth/callback" in location


def test_oauth_callback_stores_token(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "dapi-real-token"}

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("slack_agent.backend.slack_router.httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockAsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.get("/slack/oauth/callback?code=abc123&state=U123")

    assert resp.status_code == 200
    assert "Connected" in resp.text
    assert token_store.get_token("U123") == "dapi-real-token"


def test_oauth_callback_failed_exchange_returns_502(client):
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = "bad_verification_code"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("slack_agent.backend.slack_router.httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockAsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.get("/slack/oauth/callback?code=bad&state=U123")

    assert resp.status_code == 502
    assert token_store.get_token("U123") is None


def test_oauth_callback_missing_access_token_returns_502(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {}  # no access_token key

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)

    with patch("slack_agent.backend.slack_router.httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockAsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = client.get("/slack/oauth/callback?code=abc&state=U123")

    assert resp.status_code == 502
