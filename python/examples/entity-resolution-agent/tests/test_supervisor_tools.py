import pytest
from unittest.mock import MagicMock
from tests.conftest import _col, _vs_result


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("VS_ENDPOINT", "test-endpoint")
    monkeypatch.setenv("VS_INDEX_FULL", "catalog.schema.test_full_idx")
    monkeypatch.setenv("VS_INDEX_LAST_ADDR", "catalog.schema.test_last_addr_idx")
    monkeypatch.setenv("VS_INDEX_FIRST_EMAIL", "catalog.schema.test_first_email_idx")
    monkeypatch.setenv("UTILITY_ACCOUNT_TABLE", "catalog.schema.utility_accounts")


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


def test_vector_search_fans_out_across_three_indexes(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import vector_search
    result = vector_search(
        applicant_name="Jane Smith",
        address="123 Main St",
        email="",
        k=10,
        tenant_id="utility_a",
        ws=mock_ws,
    )
    # All three indexes should have been queried
    assert mock_ws.vector_search_indexes.query_index.call_count == 3
    # Results are deduplicated — same two accounts appear in all three indexes
    assert result["count"] == 2
    assert result["candidates"][0]["account_id"] == "acct-001"
    assert result["candidates"][0]["score"] == pytest.approx(0.92)


def test_vector_search_deduplicates_by_account_id(mock_ws):
    """When all three indexes return the same candidates, output is deduplicated."""
    from entity_resolution_agent.backend.core.supervisor import vector_search
    result = vector_search(applicant_name="Jane Smith", address="123 Main St", ws=mock_ws)
    # Three calls × 2 rows each = 6 raw results, but only 2 unique account IDs
    assert result["count"] == 2


def test_vector_search_keeps_highest_score_on_dedup(mock_ws):
    """If the same account appears in multiple indexes with different scores, keep the highest."""
    from entity_resolution_agent.backend.core.supervisor import vector_search
    import itertools

    rows_by_call = [
        [["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.95"]],
        [["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.80"]],
        [["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.70"]],
    ]
    call_iter = iter(rows_by_call)

    def _side_effect(**kwargs):
        return _vs_result(next(call_iter))

    mock_ws.vector_search_indexes.query_index.side_effect = _side_effect
    result = vector_search(applicant_name="Jane Smith", address="123 Main St", ws=mock_ws)
    assert result["count"] == 1
    assert result["candidates"][0]["score"] == pytest.approx(0.95)


def test_sql_search_returns_candidates(mock_ws):
    from entity_resolution_agent.backend.core.supervisor import sql_search
    from databricks.sdk.service.sql import StatementStatus, StatementState
    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [_col("account_id"), _col("name"), _col("address")]
    sql_result.result.data_array = [["acct-003", "J Smith", "456 Oak Ave"]]
    mock_ws.statement_execution.execute_statement.return_value = sql_result

    result = sql_search(name="J Smith", address="456 Oak", ws=mock_ws)
    assert result["count"] == 1
    assert result["candidates"][0]["name"] == "J Smith"
