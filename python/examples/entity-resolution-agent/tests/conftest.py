import pytest
from unittest.mock import MagicMock


def _col(name: str) -> MagicMock:
    """Create a mock schema column with .name set correctly.

    MagicMock(name=x) sets the internal _mock_name, not the .name attribute.
    Assigning after construction is the correct approach.
    """
    m = MagicMock()
    m.name = name
    return m


def _vs_result(rows: list[list]) -> MagicMock:
    """Build a fake VS query_index result with standard column schema."""
    result = MagicMock()
    result.result.data_array = rows
    result.manifest.schema.columns = [
        _col("account_id"), _col("first_name"), _col("last_name"),
        _col("service_address_line1"), _col("account_number"), _col("score"),
    ]
    return result


@pytest.fixture
def mock_ws():
    """WorkspaceClient stub with vector search and SQL execution mocked."""
    ws = MagicMock()

    # All three VS index queries return the same two candidates
    ws.vector_search_indexes.query_index.return_value = _vs_result([
        ["acct-001", "Jane", "Smith", "123 Main St", "12345", "0.92"],
        ["acct-002", "Janet", "Smyth", "123 Main Street", "12345", "0.87"],
    ])

    # SQL execution: default to empty result
    from databricks.sdk.service.sql import StatementStatus, StatementState
    sql_result = MagicMock()
    sql_result.status = StatementStatus(state=StatementState.SUCCEEDED)
    sql_result.manifest.schema.columns = [_col("account_id"), _col("name"), _col("address")]
    sql_result.result.data_array = []
    ws.statement_execution.execute_statement.return_value = sql_result

    # Warehouse listing
    wh = MagicMock()
    wh.id = "wh-001"
    wh.warehouse_type = MagicMock()
    wh.warehouse_type.__str__ = lambda self: "serverless"
    ws.warehouses.list.return_value = [wh]

    return ws
