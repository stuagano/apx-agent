"""Tests for _sql.py — get_warehouse_id() and run_sql()."""

from __future__ import annotations

from unittest.mock import MagicMock, PropertyMock

import pytest

from apx_agent._sql import get_warehouse_id, run_sql


# ---------------------------------------------------------------------------
# get_warehouse_id
# ---------------------------------------------------------------------------


class TestGetWarehouseId:
    def _make_warehouse(self, wh_id: str, wh_type: str = "") -> MagicMock:
        wh = MagicMock()
        wh.id = wh_id
        wh.warehouse_type = wh_type
        return wh

    def test_prefers_serverless(self):
        ws = MagicMock()
        ws.warehouses.list.return_value = [
            self._make_warehouse("classic-1", "CLASSIC"),
            self._make_warehouse("serverless-1", "PRO_SERVERLESS"),
        ]
        assert get_warehouse_id(ws) == "serverless-1"

    def test_falls_back_to_first_available(self):
        ws = MagicMock()
        ws.warehouses.list.return_value = [
            self._make_warehouse("classic-1", "CLASSIC"),
            self._make_warehouse("classic-2", "CLASSIC"),
        ]
        assert get_warehouse_id(ws) == "classic-1"

    def test_raises_when_none_available(self):
        ws = MagicMock()
        ws.warehouses.list.return_value = []
        with pytest.raises(RuntimeError, match="No SQL warehouse"):
            get_warehouse_id(ws)

    def test_skips_warehouses_without_id(self):
        ws = MagicMock()
        no_id = MagicMock()
        no_id.id = None
        ws.warehouses.list.return_value = [no_id, self._make_warehouse("good-1")]
        assert get_warehouse_id(ws) == "good-1"

    def test_prefer_serverless_false(self):
        ws = MagicMock()
        ws.warehouses.list.return_value = [
            self._make_warehouse("classic-1", "CLASSIC"),
            self._make_warehouse("serverless-1", "PRO_SERVERLESS"),
        ]
        # With prefer_serverless=False, should return first one with an ID
        result = get_warehouse_id(ws, prefer_serverless=False)
        assert result == "classic-1"


# ---------------------------------------------------------------------------
# run_sql
# ---------------------------------------------------------------------------


class TestRunSql:
    def _make_success_result(self, columns: list[str], rows: list[list]) -> MagicMock:
        from databricks.sdk.service.sql import StatementState

        col_mocks = []
        for c in columns:
            col = MagicMock()
            col.name = c  # set as attribute, not constructor arg
            col_mocks.append(col)

        result = MagicMock()
        result.status.state = StatementState.SUCCEEDED
        result.manifest.schema.columns = col_mocks
        result.result.data_array = rows
        return result

    def _make_ws(self, result: MagicMock) -> MagicMock:
        ws = MagicMock()
        ws.statement_execution.execute_statement.return_value = result
        return ws

    def test_returns_list_of_dicts(self):
        result = self._make_success_result(
            ["id", "name"],
            [["1", "Alice"], ["2", "Bob"]],
        )
        ws = self._make_ws(result)
        rows = run_sql(ws, "SELECT id, name FROM users", warehouse_id="wh-1")
        assert rows == [
            {"id": "1", "name": "Alice"},
            {"id": "2", "name": "Bob"},
        ]

    def test_empty_result(self):
        result = self._make_success_result(["id"], [])
        ws = self._make_ws(result)
        rows = run_sql(ws, "SELECT 1", warehouse_id="wh-1")
        assert rows == []

    def test_raises_on_failure(self):
        from databricks.sdk.service.sql import StatementState

        result = MagicMock()
        result.status.state = StatementState.FAILED
        result.status.error = "syntax error"
        ws = self._make_ws(result)
        with pytest.raises(RuntimeError, match="Query failed"):
            run_sql(ws, "BAD SQL", warehouse_id="wh-1")

    def test_auto_discovers_warehouse(self):
        """When warehouse_id is not provided, get_warehouse_id is called."""
        result = self._make_success_result(["x"], [["1"]])
        ws = self._make_ws(result)
        wh = MagicMock()
        wh.id = "auto-wh"
        wh.warehouse_type = "PRO_SERVERLESS"
        ws.warehouses.list.return_value = [wh]

        rows = run_sql(ws, "SELECT 1")
        # Verify it used the auto-discovered warehouse
        call_args = ws.statement_execution.execute_statement.call_args
        assert call_args.kwargs["warehouse_id"] == "auto-wh"

    def test_no_manifest_returns_empty(self):
        from databricks.sdk.service.sql import StatementState

        result = MagicMock()
        result.status.state = StatementState.SUCCEEDED
        result.manifest = None
        ws = self._make_ws(result)
        rows = run_sql(ws, "CREATE TABLE t", warehouse_id="wh-1")
        assert rows == []
