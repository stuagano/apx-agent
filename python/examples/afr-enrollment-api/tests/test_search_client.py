"""SQL fallback escapes user-derived ILIKE terms (claim-vs-reality on the SQL sent)."""

from __future__ import annotations

from unittest.mock import patch

from search_client import _sql_search_local


def test_sql_search_local_escapes_quotes(monkeypatch):
    monkeypatch.setenv("UTILITY_ACCOUNT_TABLE", "c.s.accounts")
    with patch("search_client.execute_sql", return_value=[]) as mock_exec:
        result = _sql_search_local("O'Brien x' OR '1'='1", "x' OR '1'='1 Main St", ws=None)

    assert result == {"candidates": [], "count": 0, "source": "live"}
    sql = mock_exec.call_args.args[0]
    assert sql == (
        "SELECT account_id, name, address FROM c.s.accounts WHERE "
        "name ILIKE '%O''Brien%' AND name ILIKE '%x''%' AND name ILIKE '%OR%' "
        "AND name ILIKE '%''1''=''1%' "
        "AND address ILIKE '%x''%' LIMIT 20"
    )
