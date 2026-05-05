import pytest
from unittest.mock import MagicMock


def _col(n: str) -> MagicMock:
    """Create a column mock with .name set as a plain attribute (not MagicMock repr-name)."""
    c = MagicMock()
    c.name = n
    return c


@pytest.fixture
def mock_ws():
    """WorkspaceClient stub with vector search and SQL execution mocked."""
    ws = MagicMock()

    # Vector search: return two fake candidates
    fake_result = MagicMock()
    fake_result.result.data_array = [
        ["acct-001", "Jane Smith", "123 Main St", "12345", "0.92"],
        ["acct-002", "Janet Smyth", "123 Main Street", "12345", "0.87"],
    ]
    fake_result.manifest.schema.columns = [
        _col("account_id"),
        _col("name"),
        _col("address"),
        _col("account_number"),
        _col("score"),
    ]
    ws.vector_search_indexes.query_index.return_value = fake_result

    # SQL execution: default to empty result
    from databricks.sdk.service.sql import StatementStatus, StatementState
    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [
        _col("account_id"),
        _col("name"),
        _col("address"),
    ]
    sql_result.result.data_array = []
    ws.statement_execution.execute_statement.return_value = sql_result

    # Warehouse listing
    wh = MagicMock()
    wh.id = "wh-001"
    wh.warehouse_type = MagicMock()
    wh.warehouse_type.__str__ = lambda self: "serverless"
    ws.warehouses.list.return_value = [wh]

    return ws
