"""Tests for the shared run_sql shim after consolidation onto databricks-tools-core.

run_sql now delegates to databricks_tools_core.execute_sql, passing the caller's
OBO WorkspaceClient so queries keep running as the end user under UC governance.
These tests pin that seam: the client is threaded through, and the
RuntimeError("Query failed: ...") contract is preserved.
"""

from unittest.mock import MagicMock, patch

import pytest

from databricks_tools_core.sql import SQLExecutionError
from tools._sql import run_sql


def test_run_sql_passes_obo_client_to_core():
    ws = MagicMock()
    with patch("tools._sql.execute_sql", return_value=[{"id": 1}]) as mock_exec:
        rows = run_sql(ws, "SELECT 1")

    assert rows == [{"id": 1}]
    mock_exec.assert_called_once()
    # OBO client threaded through as client=ws; SQL and timeout preserved.
    assert mock_exec.call_args.kwargs["client"] is ws
    assert mock_exec.call_args.kwargs["timeout"] == 30
    assert mock_exec.call_args.args[0] == "SELECT 1"


def test_run_sql_preserves_query_failed_contract():
    ws = MagicMock()
    with patch("tools._sql.execute_sql", side_effect=SQLExecutionError("boom")):
        with pytest.raises(RuntimeError, match="Query failed: boom"):
            run_sql(ws, "SELECT 1")
