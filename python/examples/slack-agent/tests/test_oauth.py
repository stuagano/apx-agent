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
