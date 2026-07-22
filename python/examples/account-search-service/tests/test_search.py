"""Unit tests for the account-search-service core search functions."""

import pytest
from unittest.mock import MagicMock, patch

from search import normalize, search, vector_search, sql_search
from tests.conftest import _col, _vs_result


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

def test_normalize_standard_name():
    result = normalize("jane smith", "123 main st")
    assert result["name"] == "Jane Smith"
    assert result["address"] == "123 Main St"
    assert result["strategy"] == "vector"


def test_normalize_initials_triggers_sql():
    result = normalize("J. Smith", "123 Main St")
    assert result["strategy"] == "sql"


def test_normalize_acronym_triggers_sql():
    result = normalize("ABC Corp", "456 Elm Ave")
    assert result["strategy"] == "sql"


# ---------------------------------------------------------------------------
# vector_search
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("VS_ENDPOINT", "test-ep")
    monkeypatch.setenv("VS_INDEX_FULL", "c.s.idx_full")
    monkeypatch.setenv("VS_INDEX_LAST_ADDR", "c.s.idx_last")
    monkeypatch.setenv("VS_INDEX_FIRST_EMAIL", "c.s.idx_email")
    monkeypatch.setenv("UTILITY_ACCOUNT_TABLE", "c.s.accounts")


def test_vector_search_fans_out_across_three_indexes(mock_ws):
    vector_search("Jane Smith", "123 Main St", ws=mock_ws)
    assert mock_ws.vector_search_indexes.query_index.call_count == 3


def test_vector_search_deduplicates_by_account_id(mock_ws):
    result = vector_search("Jane Smith", "123 Main St", ws=mock_ws)
    ids = [c["account_id"] for c in result["candidates"]]
    assert len(ids) == len(set(ids))


def test_vector_search_keeps_highest_score_on_dedup(mock_ws):
    mock_ws.vector_search_indexes.query_index.return_value = _vs_result([
        ["acct-001", "Jane", "Smith", "123 Main St", "12345", 0.92],
        ["acct-001", "Jane", "Smith", "123 Main St", "12345", 0.75],
    ])
    result = vector_search("Jane Smith", "123 Main St", ws=mock_ws)
    acct = next(c for c in result["candidates"] if c["account_id"] == "acct-001")
    assert acct["score"] == 0.92


# ---------------------------------------------------------------------------
# DEMO_MODE
# ---------------------------------------------------------------------------

def test_demo_mode_vector_search(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    result = vector_search("Jane Smith", "123 Main St", ws=None)
    assert result["source"] == "demo"
    assert len(result["candidates"]) > 0


def test_demo_mode_sql_search(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    result = sql_search("Jane Smith", ws=None)
    assert result["source"] == "demo"
    assert len(result["candidates"]) > 0


# ---------------------------------------------------------------------------
# top-level search
# ---------------------------------------------------------------------------

def test_search_routes_to_vector_for_normal_name(mock_ws):
    result = search("Jane Smith", "123 Main St", ws=mock_ws)
    assert result["strategy"] == "vector"
    assert mock_ws.vector_search_indexes.query_index.called


def test_search_routes_to_sql_for_initials(mock_ws):
    # sql_search now delegates to databricks_tools_core.execute_sql, threading
    # the OBO client (client=ws). Mock that seam and assert the client is passed.
    with patch(
        "search.execute_sql",
        return_value=[{"account_id": "acct-010", "name": "J. Williams", "address": "55 Oak St"}],
    ) as mock_exec:
        result = search("J. Williams", "55 Oak St", ws=mock_ws)

    assert result["strategy"] == "sql"
    assert result["candidates"] == [
        {"account_id": "acct-010", "name": "J. Williams", "address": "55 Oak St"}
    ]
    assert mock_exec.call_args.kwargs["client"] is mock_ws
    mock_ws.vector_search_indexes.query_index.assert_not_called()
