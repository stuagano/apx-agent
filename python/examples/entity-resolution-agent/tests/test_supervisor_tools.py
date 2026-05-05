import os
import pytest
from unittest.mock import MagicMock


def _col(n):
    """Create a column mock with .name attribute."""
    c = MagicMock()
    c.name = n
    return c


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("VECTOR_SEARCH_ENDPOINT_NAME", "test-endpoint")
    monkeypatch.setenv("VECTOR_SEARCH_INDEX_NAME", "catalog.schema.test_idx")
    monkeypatch.setenv("ACCOUNT_TABLE", "catalog.schema.accounts")


def test_normalize_record_basic(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import normalize_record
    result = normalize_record(
        name="  jane smith  ",
        address="123 main st apt 2",
        account_number=" 12345 ",
        ws=mock_ws,
    )
    assert result["name"] == "Jane Smith"
    assert result["address"] == "123 Main St Apt 2"
    assert result["account_number"] == "12345"
    assert result["strategy"] == "vector"


def test_normalize_record_initials_triggers_sql(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import normalize_record
    result = normalize_record(name="J. Smith", address="", account_number="", ws=mock_ws)
    assert result["strategy"] == "sql"


def test_normalize_record_acronym_triggers_sql(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import normalize_record
    result = normalize_record(name="ABC LLC", address="", account_number="", ws=mock_ws)
    assert result["strategy"] == "sql"


def test_vector_search_returns_candidates(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import vector_search
    result = vector_search(query="Jane Smith 123 Main St", k=10, ws=mock_ws)
    assert result["count"] == 2
    assert result["candidates"][0]["account_id"] == "acct-001"
    assert result["candidates"][0]["score"] == pytest.approx(0.92)
    mock_ws.vector_search_indexes.query_index.assert_called_once()


def test_sql_search_returns_candidates(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import sql_search
    from databricks.sdk.service.sql import StatementStatus, StatementState
    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [
        _col("account_id"),
        _col("name"),
        _col("address"),
    ]
    sql_result.result.data_array = [["acct-003", "J Smith", "456 Oak Ave"]]
    mock_ws.statement_execution.execute_statement.return_value = sql_result

    result = sql_search(name="J Smith", address="456 Oak", ws=mock_ws)
    assert result["count"] == 1
    assert result["candidates"][0]["name"] == "J Smith"
