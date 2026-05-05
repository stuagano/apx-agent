import importlib
import pytest


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_SERVICE_ACCOUNT_EMAIL", "bot@test.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    monkeypatch.setenv("JIRA_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("DATA_TRIAGE_JOB_ID", "42")
    monkeypatch.setenv("DATA_INSPECTOR_URL", "https://inspector.example.com")

    from data_triage_agent.backend.config import Settings
    s = Settings()

    assert s.jira_base_url == "https://test.atlassian.net"
    assert s.jira_service_account_email == "bot@test.com"
    assert s.jira_api_token == "tok"
    assert s.jira_webhook_secret == "secret"
    assert s.data_triage_job_id == 42
    assert s.data_inspector_url == "https://inspector.example.com"


def test_settings_defaults():
    from data_triage_agent.backend.config import Settings
    s = Settings()
    assert s.data_inspector_url == "http://localhost:9000"
    assert s.data_triage_job_id == 0


def test_get_settings_is_singleton(monkeypatch):
    import data_triage_agent.backend.config as cfg_mod
    # Reset the singleton so we get a fresh instance
    cfg_mod._settings = None
    monkeypatch.setenv("JIRA_BASE_URL", "https://singleton.atlassian.net")

    s1 = cfg_mod.get_settings()
    s2 = cfg_mod.get_settings()

    assert s1 is s2
    assert s1.jira_base_url == "https://singleton.atlassian.net"

    # Cleanup for test isolation
    cfg_mod._settings = None
