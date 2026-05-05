from unittest.mock import MagicMock
from tools.discover_tables import search_tables, list_genie_spaces


def test_search_tables_returns_dot_separated_identifiers():
    sql = MagicMock(return_value=[
        {"table_catalog": "main", "table_schema": "sales", "table_name": "orders", "comment": "Order records"},
    ])
    result = search_tables("orders", sql)
    assert result == [{"identifier": "main.sales.orders", "comment": "Order records"}]


def test_search_tables_uses_bind_parameters():
    sql = MagicMock(return_value=[])
    search_tables("test", sql)
    call_args = sql.call_args
    query = call_args[0][0]
    assert ":pattern" in query
    params = call_args[1].get("parameters") or call_args[0][1]
    assert any(p["name"] == "pattern" for p in params)


def test_search_tables_empty_results():
    sql = MagicMock(return_value=[])
    assert search_tables("nonexistent", sql) == []


def test_list_genie_spaces_returns_id_and_name():
    ws = MagicMock()
    ws.api_client.do.return_value = {
        "spaces": [{"space_id": "abc123", "title": "Sales Analytics"}]
    }
    result = list_genie_spaces(ws)
    assert result == [{"id": "abc123", "name": "Sales Analytics"}]


def test_list_genie_spaces_empty_workspace():
    ws = MagicMock()
    ws.api_client.do.return_value = {}
    assert list_genie_spaces(ws) == []
