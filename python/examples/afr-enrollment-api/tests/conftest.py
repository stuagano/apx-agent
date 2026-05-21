"""Shared fixtures for afr-enrollment-api tests."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock


def _col(name: str) -> MagicMock:
    col = MagicMock()
    col.name = name
    return col


def _vs_result(rows: list[list]) -> MagicMock:
    result = MagicMock()
    result.manifest.schema.columns = [
        _col("account_id"), _col("first_name"), _col("last_name"),
        _col("service_address_line1"), _col("account_number"), _col("score"),
    ]
    result.result.data_array = rows
    return result


@pytest.fixture
def mock_ws():
    ws = MagicMock()
    ws.vector_search_indexes.query_index.return_value = _vs_result([
        ["acct-001", "Jane", "Smith", "123 Main St", "12345", 0.92],
        ["acct-002", "John", "Smith", "123 Main St", "12346", 0.87],
    ])
    # Mock a serverless SQL warehouse so evaluator.log_decision can pick one up.
    wh = MagicMock()
    wh.id = "wh-test"
    wh.warehouse_type = "serverless"
    ws.warehouses.list.return_value = [wh]
    # SQL execute defaults to a benign success.
    from databricks.sdk.service.sql import StatementState, StatementStatus
    res = MagicMock()
    res.status = StatementStatus(state=StatementState.SUCCEEDED)
    res.manifest.schema.columns = [_col("account_id"), _col("name"), _col("address")]
    res.result.data_array = []
    ws.statement_execution.execute_statement.return_value = res
    return ws
