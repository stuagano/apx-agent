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
    monkeypatch.setenv("SEARCH_SERVICE_URL", "")   # use local path in tests


def test_normalize_record_basic(mock_ws):
    from sub_agents.supervisor.agent import normalize_record
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
    from sub_agents.supervisor.agent import normalize_record
    result = normalize_record(name="J. Smith", address="", account_number="", ws=mock_ws)
    assert result["strategy"] == "sql"


def test_normalize_record_acronym_triggers_sql(mock_ws):
    from sub_agents.supervisor.agent import normalize_record
    result = normalize_record(name="ABC LLC", address="", account_number="", ws=mock_ws)
    assert result["strategy"] == "sql"


def test_search_accounts_fans_out_across_three_indexes(mock_ws):
    from sub_agents.supervisor.agent import search_accounts
    search_accounts(applicant_name="Jane Smith", address="123 Main St", ws=mock_ws)
    assert mock_ws.vector_search_indexes.query_index.call_count == 3


def test_search_accounts_deduplicates_by_account_id(mock_ws):
    from sub_agents.supervisor.agent import search_accounts
    result = search_accounts(applicant_name="Jane Smith", address="123 Main St", ws=mock_ws)
    ids = [c["account_id"] for c in result["candidates"]]
    assert len(ids) == len(set(ids))


def test_search_accounts_keeps_highest_score_on_dedup(mock_ws):
    from sub_agents.supervisor.agent import search_accounts

    rows_by_call = [
        [["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.95"]],
        [["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.80"]],
        [["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.70"]],
    ]
    call_iter = iter(rows_by_call)

    def _side_effect(**kwargs):
        return _vs_result(next(call_iter))

    mock_ws.vector_search_indexes.query_index.side_effect = _side_effect
    result = search_accounts(applicant_name="Jane Smith", address="123 Main St", ws=mock_ws)
    assert result["count"] == 1
    assert result["candidates"][0]["score"] == pytest.approx(0.95)


def test_search_accounts_sql_path_for_initials(mock_ws):
    """Names with initials trigger the SQL fallback via _sql_fallback."""
    from sub_agents.supervisor.agent import search_accounts
    from databricks.sdk.service.sql import StatementStatus, StatementState

    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [_col("account_id"), _col("name"), _col("address")]
    sql_result.result.data_array = [["acct-010", "J. Williams", "55 Oak St"]]
    mock_ws.statement_execution.execute_statement.return_value = sql_result

    # Clear VS indexes so the local path falls through to SQL
    import os
    import pytest
    with pytest.MonkeyPatch().context() as mp:
        mp.delenv("VS_INDEX_FULL", raising=False)
        mp.delenv("VS_INDEX_LAST_ADDR", raising=False)
        mp.delenv("VS_INDEX_FIRST_EMAIL", raising=False)
        result = search_accounts(applicant_name="J. Williams", address="55 Oak St", ws=mock_ws)

    mock_ws.vector_search_indexes.query_index.assert_not_called()
    assert result["count"] == 1


def test_search_accounts_demo_mode(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    from sub_agents.supervisor.agent import search_accounts
    result = search_accounts(applicant_name="Jane Smith", address="123 Maple Ave", ws=None)
    assert result["source"] == "demo"
    assert len(result["candidates"]) > 0
