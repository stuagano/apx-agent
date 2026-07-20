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

    def test_no_warehouse_error_includes_workspace_url_and_fix(self):
        ws = MagicMock()
        ws.config.host = "https://my-workspace.azuredatabricks.net"
        ws.warehouses.list.return_value = []
        with pytest.raises(RuntimeError) as exc:
            get_warehouse_id(ws)
        msg = str(exc.value)
        assert "my-workspace.azuredatabricks.net/sql/warehouses" in msg
        assert "Create Warehouse" in msg

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
        # Single-chunk by default: no further chunks to follow. Without this the
        # MagicMock auto-attribute is truthy and the pagination loop never ends.
        result.result.next_chunk_index = None
        result.statement_id = "stmt-1"
        return result

    def _make_ws(self, result: MagicMock) -> MagicMock:
        from databricks.sdk.service.sql import State

        ws = MagicMock()
        ws.statement_execution.execute_statement.return_value = result
        # Report the warehouse as already RUNNING so _ensure_warehouse_running()
        # is a no-op. Without this, the MagicMock's default .state never equals
        # State.RUNNING, so the cold-start poll loop runs the full 60s timeout —
        # which made each of these tests take 60s (300s of the suite's runtime).
        ws.warehouses.get.return_value.state = State.RUNNING
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

    def test_follows_next_chunk_index_across_chunks(self):
        """Regression for #228: rows past the first inline chunk aren't dropped."""
        result = self._make_success_result(["id"], [["1"], ["2"]])
        result.result.next_chunk_index = 1
        ws = self._make_ws(result)

        chunk1 = MagicMock()
        chunk1.data_array = [["3"], ["4"]]
        chunk1.next_chunk_index = 2
        chunk2 = MagicMock()
        chunk2.data_array = [["5"]]
        chunk2.next_chunk_index = None
        ws.statement_execution.get_statement_result_chunk_n.side_effect = [chunk1, chunk2]

        rows = run_sql(ws, "SELECT id FROM big", warehouse_id="wh-1")
        assert rows == [{"id": v} for v in ("1", "2", "3", "4", "5")]
        # Fetched chunk 1 then chunk 2 by statement id + next index, stopping at None.
        calls = ws.statement_execution.get_statement_result_chunk_n.call_args_list
        assert [c.args for c in calls] == [("stmt-1", 1), ("stmt-1", 2)]

    def test_single_chunk_does_not_fetch_more(self):
        """next_chunk_index=None ⇒ no chunk fetches (the common small-result path)."""
        result = self._make_success_result(["id"], [["1"]])
        ws = self._make_ws(result)
        rows = run_sql(ws, "SELECT id FROM small", warehouse_id="wh-1")
        assert rows == [{"id": "1"}]
        ws.statement_execution.get_statement_result_chunk_n.assert_not_called()

    def test_polls_to_completion_when_still_running_after_wait_timeout(self):
        """execute_statement's wait_timeout caps the synchronous wait at 30s -
        a genuinely slow query (not a cold warehouse - _ensure_warehouse_running
        already handles that) can come back RUNNING with no error. run_sql must
        poll get_statement() to a real terminal state instead of treating
        still-running as a failure."""
        from databricks.sdk.service.sql import StatementState

        pending = MagicMock()
        pending.status.state = StatementState.RUNNING
        pending.statement_id = "stmt-slow"
        ws = self._make_ws(pending)

        done = self._make_success_result(["id"], [["1"]])
        done.statement_id = "stmt-slow"
        ws.statement_execution.get_statement.return_value = done

        rows = run_sql(
            ws, "SELECT id FROM slow_join", warehouse_id="wh-1",
            poll_interval_s=0.01, poll_timeout_s=1,
        )
        assert rows == [{"id": "1"}]
        ws.statement_execution.get_statement.assert_called_with("stmt-slow")

    def test_raises_clear_timeout_message_when_still_running_after_poll_budget(self):
        """A query that's STILL running after the full poll budget must not
        report the misleading 'unknown error' (status.error is None for a
        running statement, not a failure) - the message must say it's still
        running, not that it failed, so the LLM's retry logic doesn't loop on
        a non-existent syntax error."""
        from databricks.sdk.service.sql import StatementState

        pending = MagicMock()
        pending.status.state = StatementState.RUNNING
        pending.status.error = None
        pending.statement_id = "stmt-forever"
        ws = self._make_ws(pending)
        ws.statement_execution.get_statement.return_value = pending

        with pytest.raises(RuntimeError) as exc:
            run_sql(
                ws, "SELECT * FROM huge_join", warehouse_id="wh-1",
                poll_interval_s=0.01, poll_timeout_s=0.03,
            )
        msg = str(exc.value)
        assert "still running" in msg.lower()
        assert "unknown error" not in msg.lower()
        assert "stmt-forever" in msg

    def test_genuine_failure_after_polling_still_reports_real_error(self):
        """A statement that transitions RUNNING -> FAILED during polling must
        still surface the real error message, not the timeout message."""
        from databricks.sdk.service.sql import StatementState

        pending = MagicMock()
        pending.status.state = StatementState.RUNNING
        pending.statement_id = "stmt-2"
        ws = self._make_ws(pending)

        failed = MagicMock()
        failed.status.state = StatementState.FAILED
        failed.status.error = "table not found"
        ws.statement_execution.get_statement.return_value = failed

        with pytest.raises(RuntimeError, match="table not found"):
            run_sql(
                ws, "SELECT * FROM gone", warehouse_id="wh-1",
                poll_interval_s=0.01, poll_timeout_s=1,
            )

    def test_scope_denial_on_execute_reraises_with_reauthorize_hint(self):
        """#556: a 'required scopes: sql' 403 from execute_statement must be
        re-raised with re-authorize guidance, not a bare PermissionDenied."""
        ws = self._make_ws(MagicMock())
        ws.statement_execution.execute_statement.side_effect = Exception(
            "Invalid scope, required scopes: sql"
        )
        with pytest.raises(RuntimeError) as exc:
            run_sql(ws, "SELECT 1", warehouse_id="wh-1")
        msg = str(exc.value)
        assert "re-authorize" in msg.lower()
        assert "required scopes: sql" in msg  # original detail preserved

    def test_scope_denial_during_warehouse_discovery_also_hinted(self):
        """The 403 can originate in warehouses.list() (warehouse auto-discovery),
        not just execute_statement — that path gets the same hint."""
        ws = self._make_ws(MagicMock())
        ws.warehouses.list.side_effect = Exception(
            "Invalid scope, required scopes: sql"
        )
        with pytest.raises(RuntimeError, match="(?i)re-authorize"):
            run_sql(ws, "SELECT 1")  # no warehouse_id → triggers discovery

    def test_non_scope_error_is_not_rewritten(self):
        """A normal failure (not a scope denial) passes through unchanged."""
        ws = self._make_ws(MagicMock())
        ws.statement_execution.execute_statement.side_effect = Exception("boom")
        with pytest.raises(Exception, match="boom") as exc:
            run_sql(ws, "SELECT 1", warehouse_id="wh-1")
        assert "re-authorize" not in str(exc.value).lower()
