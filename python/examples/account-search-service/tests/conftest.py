"""Shared fixtures for account-search-service tests."""

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
    return ws
