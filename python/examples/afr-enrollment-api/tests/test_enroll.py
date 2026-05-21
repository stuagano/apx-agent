"""Tests for POST /api/enroll in afr-enrollment-api."""

import typing
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from apx_agent import Dependencies, create_app
from app import app as _base_app
from tests.conftest import _col, _vs_result

_WS_DEP_FUNC = typing.get_args(Dependencies.Workspace)[1].dependency


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("SEARCH_SERVICE_URL", "")   # local search fallback
    monkeypatch.setenv("VS_ENDPOINT", "test-ep")
    monkeypatch.setenv("VS_INDEX_FULL", "c.s.idx_full")
    monkeypatch.setenv("VS_INDEX_LAST_ADDR", "c.s.idx_last")
    monkeypatch.setenv("VS_INDEX_FIRST_EMAIL", "c.s.idx_email")
    monkeypatch.setenv("UTILITY_ACCOUNT_TABLE", "c.s.accounts")
    monkeypatch.setenv("AFR_DECISION_TABLE", "c.s.afr_processing")


@pytest.fixture
def client(mock_ws):
    from databricks.sdk.service.sql import StatementStatus, StatementState
    log_result = MagicMock()
    log_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    log_result.manifest.schema.columns = [_col("account_id")]
    log_result.result.data_array = []
    mock_ws.statement_execution.execute_statement.return_value = log_result

    _base_app.dependency_overrides[_WS_DEP_FUNC] = lambda: mock_ws
    yield TestClient(_base_app)
    _base_app.dependency_overrides.clear()


def test_enroll_happy_path(client):
    resp = client.post("/api/enroll", json={
        "applicant_name": "Jane Smith",
        "address": "123 Main St",
        "email": "jane@example.com",
        "account_number": "12345",
        "tenant_id": "utility_a",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is True
    assert data["account_id"] == "acct-001"
    assert data["category"] in ("EXACT", "HIGH_CONFIDENCE")
    assert data["confidence"] >= 0.87


def test_enroll_no_match(client, mock_ws):
    mock_ws.vector_search_indexes.query_index.return_value = _vs_result([])
    resp = client.post("/api/enroll", json={
        "applicant_name": "Nobody Real",
        "address": "999 Missing Ln",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is False
    assert data["category"] == "NO_MATCH"
    assert data["account_id"] is None


def test_enroll_missing_name_returns_422(client):
    resp = client.post("/api/enroll", json={"address": "123 Main St"})
    assert resp.status_code == 422


def test_demo_mode_enroll(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("AFR_DECISION_TABLE", "c.s.afr_processing")
    mock_ws = MagicMock()
    _base_app.dependency_overrides[_WS_DEP_FUNC] = lambda: mock_ws
    try:
        c = TestClient(_base_app)
        resp = c.post("/api/enroll", json={"applicant_name": "Jane Smith", "address": "123 Maple Ave"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched"] is True or data["category"] == "NO_MATCH"
    finally:
        _base_app.dependency_overrides.clear()
