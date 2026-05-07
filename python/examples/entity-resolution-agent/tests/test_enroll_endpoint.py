"""Integration tests for POST /api/enroll.

The endpoint calls supervisor tools then evaluator tools in a deterministic
path — no LLM. Tests override the apx_agent workspace dependency to inject
mock_ws instead of a real WorkspaceClient.
"""

import typing
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from apx_agent import Dependencies, create_app
from entity_resolution_agent.backend.agent_router import agent
from entity_resolution_agent.backend.router import router
from tests.conftest import _vs_result, _col

# Extract the underlying callable from the Annotated[..., Depends(...)] type
_WS_DEP_FUNC = typing.get_args(Dependencies.Workspace)[1].dependency


@pytest.fixture
def app(mock_ws):
    """FastAPI app with workspace dependency overridden to use mock_ws."""
    _app = create_app(agent)
    _app.include_router(router)
    _app.dependency_overrides[_WS_DEP_FUNC] = lambda: mock_ws
    yield _app
    _app.dependency_overrides.clear()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("VS_ENDPOINT", "test-endpoint")
    monkeypatch.setenv("VS_INDEX_FULL", "catalog.schema.test_full_idx")
    monkeypatch.setenv("VS_INDEX_LAST_ADDR", "catalog.schema.test_last_addr_idx")
    monkeypatch.setenv("VS_INDEX_FIRST_EMAIL", "catalog.schema.test_first_email_idx")
    monkeypatch.setenv("UTILITY_ACCOUNT_TABLE", "catalog.schema.utility_accounts")
    monkeypatch.setenv("AFR_DECISION_TABLE", "catalog.schema.afr_processing")


def test_enroll_happy_path(client, mock_ws):
    """Standard full-name match — vector search returns a high-confidence candidate."""
    from databricks.sdk.service.sql import StatementStatus, StatementState
    log_result = MagicMock()
    log_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    log_result.manifest.schema.columns = [_col("account_id")]
    log_result.result.data_array = []
    mock_ws.statement_execution.execute_statement.return_value = log_result

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
    assert data["candidates_reviewed"] == 2


def test_enroll_no_match(client, mock_ws):
    """No candidates returned — should produce a NO_MATCH decision."""
    mock_ws.vector_search_indexes.query_index.return_value = _vs_result([])

    from databricks.sdk.service.sql import StatementStatus, StatementState
    log_result = MagicMock()
    log_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    log_result.manifest.schema.columns = [_col("account_id")]
    log_result.result.data_array = []
    mock_ws.statement_execution.execute_statement.return_value = log_result

    resp = client.post("/api/enroll", json={
        "applicant_name": "Nobody Real",
        "address": "999 Missing Ln",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is False
    assert data["category"] == "NO_MATCH"
    assert data["account_id"] is None


def test_enroll_sql_fallback_for_initials(client, mock_ws):
    """Name with initials triggers SQL search strategy instead of vector search."""
    from databricks.sdk.service.sql import StatementStatus, StatementState
    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [_col("account_id"), _col("name"), _col("address")]
    sql_result.result.data_array = [["acct-010", "J. Williams", "55 Oak St"]]
    mock_ws.statement_execution.execute_statement.return_value = sql_result

    resp = client.post("/api/enroll", json={
        "applicant_name": "J. Williams",
        "address": "55 Oak St",
    })
    assert resp.status_code == 200
    # Vector search should NOT have been called
    mock_ws.vector_search_indexes.query_index.assert_not_called()
    data = resp.json()
    assert data["candidates_reviewed"] >= 1


def test_enroll_returns_422_for_missing_name(client):
    """applicant_name is required — missing it should return 422."""
    resp = client.post("/api/enroll", json={"address": "123 Main St"})
    assert resp.status_code == 422
