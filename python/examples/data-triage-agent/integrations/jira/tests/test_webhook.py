import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from app import app
from integrations.jira.config import Settings, get_settings


SECRET = "test-secret-1234"
JOB_ID = 7


def _make_settings():
    return Settings(
        jira_webhook_secret=SECRET,
        data_triage_job_id=JOB_ID,
        jira_base_url="https://test.atlassian.net",
        jira_service_account_email="bot@test.com",
        jira_api_token="tok",
        data_inspector_url="http://localhost:9000",
    )


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_payload(issuetype="Data Issue", event="jira:issue_created") -> dict:
    return {
        "webhookEvent": event,
        "issue": {
            "key": "DATA-42",
            "fields": {
                "summary": "Missing billing data for account 1234",
                "description": None,
                "issuetype": {"name": issuetype},
                "priority": {"name": "High"},
                "reporter": {"displayName": "Alice"},
            },
        },
    }


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_valid_webhook_returns_202(client):
    payload = json.dumps(_make_payload()).encode()
    with patch("integrations.jira.webhook.WorkspaceClient") as MockWs:
        mock_ws = MagicMock()
        MockWs.return_value = mock_ws
        resp = client.post(
            "/webhook/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": _sign(payload, SECRET),
            },
        )
    assert resp.status_code == 202
    mock_ws.jobs.run_now.assert_called_once()


def test_invalid_signature_returns_401(client):
    payload = json.dumps(_make_payload()).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": "sha256=bad",
        },
    )
    assert resp.status_code == 401


def test_wrong_event_returns_200(client):
    payload = json.dumps(_make_payload(event="jira:issue_updated")).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": _sign(payload, SECRET),
        },
    )
    assert resp.status_code == 200


def test_wrong_issuetype_returns_200(client):
    payload = json.dumps(_make_payload(issuetype="Bug")).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": _sign(payload, SECRET),
        },
    )
    assert resp.status_code == 200


def test_missing_signature_returns_401(client):
    payload = json.dumps(_make_payload()).encode()
    resp = client.post(
        "/webhook/jira",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_run_now_called_with_issue_key(client):
    payload = json.dumps(_make_payload()).encode()
    with patch("integrations.jira.webhook.WorkspaceClient") as MockWs:
        mock_ws = MagicMock()
        MockWs.return_value = mock_ws
        client.post(
            "/webhook/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": _sign(payload, SECRET),
            },
        )
    call_kwargs = mock_ws.jobs.run_now.call_args
    python_params = call_kwargs.kwargs.get("python_params") or []
    assert "--issue-key" in python_params
    assert "DATA-42" in python_params
    assert "--summary" in python_params


def test_adf_description_extracted(client):
    adf_description = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "We see 0 rows for account 1234."}],
            }
        ],
    }
    payload_dict = _make_payload()
    payload_dict["issue"]["fields"]["description"] = adf_description
    payload = json.dumps(payload_dict).encode()
    with patch("integrations.jira.webhook.WorkspaceClient") as MockWs:
        mock_ws = MagicMock()
        MockWs.return_value = mock_ws
        client.post(
            "/webhook/jira",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": _sign(payload, SECRET),
            },
        )
    call_kwargs = mock_ws.jobs.run_now.call_args
    python_params = call_kwargs.kwargs.get("python_params") or []
    assert "--description" in python_params
    idx = python_params.index("--description")
    assert "0 rows for account 1234" in python_params[idx + 1]
