from unittest.mock import MagicMock, patch

from tools.query_portfolio import query_portfolio


def test_query_portfolio_builds_safe_filtered_sql():
    ws = MagicMock()
    with patch(
        "tools.query_portfolio.run_sql",
        return_value=[{"contract_id": "CT-0001", "counterparty": "Acme Energy Co"}],
    ) as mock_sql:
        out = query_portfolio(
            counterparty="Acme Energy Co",
            contract_type="ppa",
            auto_renewal=True,
            expires_within_days=90,
            ws=ws,
        )
    assert out["count"] == 1
    sql = mock_sql.call_args[0][1]
    assert "counterparty = 'Acme Energy Co'" in sql
    assert "contract_type = 'ppa'" in sql
    assert "auto_renewal = true" in sql
    assert "expiry_date <= date_add(current_date(), 90)" in sql
    assert "expiry_date >= current_date()" in sql


def test_query_portfolio_expiry_window_only():
    ws = MagicMock()
    with patch(
        "tools.query_portfolio.run_sql",
        return_value=[{"contract_id": "CT-0001"}],
    ) as mock_sql:
        out = query_portfolio(expires_within_days=30, ws=ws)
    assert out["count"] == 1
    sql = mock_sql.call_args[0][1]
    assert "date_add(current_date(), 30)" in sql
    assert "expiry_date >= current_date()" in sql
    assert "ORDER BY expiry_date" in sql


def test_query_portfolio_empty_filters():
    ws = MagicMock()
    with patch(
        "tools.query_portfolio.run_sql",
        return_value=[],
    ):
        out = query_portfolio(ws=ws)
    assert out == {"rows": [], "count": 0}


def test_query_portfolio_rejects_sql_injection_in_counterparty():
    ws = MagicMock()
    with patch(
        "tools.query_portfolio.run_sql",
        return_value=[],
    ) as mock_sql:
        query_portfolio(counterparty="Acme Energy Co'; DROP TABLE x; --", ws=ws)
    sql = mock_sql.call_args[0][1]
    # Quote should be escaped, not terminate the literal.
    assert "DROP TABLE" not in sql.upper().split("'")[0]
