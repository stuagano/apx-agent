import hashlib
import hmac
import time
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from slack_agent.backend.app import app
from slack_agent.backend.config import Settings, get_settings
from slack_agent.backend import token_store

SECRET = "signing-secret"
APP_URL = "https://my-app.databricksapps.com"


def _make_settings():
    return Settings(
        databricks_host="adb-123.azuredatabricks.net",
        databricks_client_id="client-id",
        databricks_client_secret="client-secret",
        app_url=APP_URL,
        slack_signing_secret=SECRET,
        slack_bot_token="xoxb-bot-token",
    )


def _sign(body: bytes, timestamp: str) -> str:
    basestring = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(SECRET.encode(), basestring.encode(), hashlib.sha256).hexdigest()


def _slash(command: str = "/whoami", user_id: str = "U123", text: str = "") -> tuple[bytes, dict]:
    body = (
        f"command={command}&user_id={user_id}"
        f"&text={text}&response_url=https://hooks.slack.com/resp/abc"
    ).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(body, ts),
    }
    return body, headers


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


def test_invalid_signature_returns_401(client):
    body = b"command=%2Fwhoami&user_id=U123"
    ts = str(int(time.time()))
    resp = client.post(
        "/slack/events",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": "v0=bad",
        },
    )
    assert resp.status_code == 401


def test_connect_command_returns_install_link(client):
    body, headers = _slash(command="/connect", user_id="U123")
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    assert "/slack/install" in data["text"]
    assert "U123" in data["text"]


def test_command_without_stored_token_returns_connect_prompt(client):
    body, headers = _slash(command="/whoami", user_id="U456")
    resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    assert "install" in data["text"].lower() or "connect" in data["text"].lower()


def test_command_with_token_returns_200_and_fires_task(client):
    token_store.set_token("U123", "dapi-fake-token")
    body, headers = _slash(command="/whoami", user_id="U123")
    with patch("slack_agent.backend.slack_router.asyncio.create_task") as mock_task:
        resp = client.post("/slack/events", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["response_type"] == "ephemeral"
    mock_task.assert_called_once()


def test_command_with_token_passes_correct_args_to_dispatch(client):
    token_store.set_token("U123", "dapi-real-token")
    body, headers = _slash(command="/whoami", user_id="U123", text="")
    captured = {}

    def capture_task(coro):
        captured["coro"] = coro
        coro.close()  # prevent ResourceWarning

    with patch("slack_agent.backend.slack_router.asyncio.create_task", side_effect=capture_task):
        client.post("/slack/events", content=body, headers=headers)

    assert "coro" in captured
