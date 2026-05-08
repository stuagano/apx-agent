import pytest
from slack_agent.backend.config import Settings


def test_settings_defaults():
    s = Settings()
    assert s.databricks_host == ""
    assert s.databricks_client_id == ""
    assert s.databricks_client_secret == ""
    assert s.app_url == ""
    assert s.slack_signing_secret == ""
    assert s.slack_bot_token == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "adb-123.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "my-client-id")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "my-signing-secret")
    s = Settings()
    assert s.databricks_host == "adb-123.azuredatabricks.net"
    assert s.databricks_client_id == "my-client-id"
    assert s.slack_signing_secret == "my-signing-secret"
