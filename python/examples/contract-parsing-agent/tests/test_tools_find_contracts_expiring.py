from unittest.mock import MagicMock, patch

from tools.find_contracts_expiring import (
    find_contracts_expiring,
)


def test_find_contracts_expiring_default_window():
    ws = MagicMock()
    with patch(
        "tools.find_contracts_expiring.run_sql",
        return_value=[{"contract_id": "CT-0001"}],
    ) as mock_sql:
        out = find_contracts_expiring(ws=ws)
    assert out["count"] == 1
    sql = mock_sql.call_args[0][1]
    assert "date_add(current_date(), 90)" in sql
    assert "ORDER BY expiry_date" in sql


def test_find_contracts_expiring_custom_window():
    ws = MagicMock()
    with patch(
        "tools.find_contracts_expiring.run_sql",
        return_value=[],
    ) as mock_sql:
        find_contracts_expiring(within_days=30, ws=ws)
    sql = mock_sql.call_args[0][1]
    assert "date_add(current_date(), 30)" in sql
