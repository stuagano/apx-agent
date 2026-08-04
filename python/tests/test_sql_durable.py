"""Tests for restart-safe Databricks SQL execution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from databricks.sdk.service.sql import State, StatementState

from apx_agent import run_sql_durable


def _response(
    state: StatementState,
    *,
    statement_id: str = "stmt-1",
    columns: list[str] | None = None,
    rows: list[list[str]] | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status.state = state
    response.status.error = None
    response.statement_id = statement_id
    if state == StatementState.SUCCEEDED:
        response.manifest.schema.columns = []
        for name in columns or []:
            column = MagicMock()
            column.name = name
            response.manifest.schema.columns.append(column)
        response.result.data_array = rows or []
        response.result.next_chunk_index = None
    return response


def _workspace() -> MagicMock:
    ws = MagicMock()
    ws.warehouses.get.return_value.state = State.RUNNING
    return ws


def test_restart_resumes_checkpointed_statement_without_resubmitting(tmp_path: Path) -> None:
    ws = _workspace()
    pending = _response(StatementState.RUNNING, statement_id="stmt-slow")
    ws.statement_execution.execute_statement.return_value = pending
    ws.statement_execution.get_statement.return_value = pending

    with pytest.raises(RuntimeError, match="still running"):
        run_sql_durable(
            ws,
            "SELECT * FROM slow_table",
            warehouse_id="wh-1",
            checkpoint_dir=tmp_path,
            poll_timeout_s=0,
            poll_interval_s=0,
        )

    ws.statement_execution.execute_statement.reset_mock()
    ws.statement_execution.get_statement.return_value = _response(
        StatementState.SUCCEEDED,
        statement_id="stmt-slow",
        columns=["id"],
        rows=[["42"]],
    )

    rows = run_sql_durable(
        ws,
        "SELECT * FROM slow_table",
        warehouse_id="wh-1",
        checkpoint_dir=tmp_path,
        poll_timeout_s=1,
        poll_interval_s=0,
    )

    assert rows == [{"id": "42"}]
    ws.statement_execution.execute_statement.assert_not_called()
    ws.statement_execution.get_statement.assert_called_with("stmt-slow")


def test_succeeded_result_is_reused_from_checkpoint(tmp_path: Path) -> None:
    ws = _workspace()
    ws.statement_execution.execute_statement.return_value = _response(
        StatementState.SUCCEEDED,
        columns=["answer"],
        rows=[["yes"]],
    )

    first = run_sql_durable(
        ws,
        "SELECT 'yes' AS answer",
        warehouse_id="wh-1",
        checkpoint_dir=tmp_path,
    )
    ws.reset_mock()
    second = run_sql_durable(
        ws,
        "SELECT 'yes' AS answer",
        warehouse_id="wh-1",
        checkpoint_dir=tmp_path,
    )

    assert first == second == [{"answer": "yes"}]
    ws.statement_execution.execute_statement.assert_not_called()
    ws.statement_execution.get_statement.assert_not_called()


def test_progress_callback_reports_submission_and_poll_state(tmp_path: Path) -> None:
    ws = _workspace()
    ws.statement_execution.execute_statement.return_value = _response(
        StatementState.RUNNING
    )
    ws.statement_execution.get_statement.return_value = _response(
        StatementState.SUCCEEDED,
        columns=["id"],
        rows=[["1"]],
    )
    progress: list[tuple[str, float]] = []

    run_sql_durable(
        ws,
        "SELECT 1 AS id",
        warehouse_id="wh-1",
        checkpoint_dir=tmp_path,
        poll_timeout_s=1,
        poll_interval_s=0,
        on_progress=lambda state, elapsed: progress.append((state, elapsed)),
    )

    assert [state for state, _ in progress] == ["SUBMITTED", "RUNNING", "SUCCEEDED"]


def test_running_state_is_written_before_poll_timeout(tmp_path: Path) -> None:
    ws = _workspace()
    running = _response(StatementState.RUNNING, statement_id="stmt-running")
    ws.statement_execution.execute_statement.return_value = running

    with pytest.raises(RuntimeError, match="still running"):
        run_sql_durable(
            ws,
            "SELECT * FROM long_job",
            warehouse_id="wh-1",
            checkpoint_dir=tmp_path,
            poll_timeout_s=0,
            poll_interval_s=0,
        )

    checkpoints = list(tmp_path.glob("*.json"))
    assert len(checkpoints) == 1
    assert json.loads(checkpoints[0].read_text())["state"] == "RUNNING"
