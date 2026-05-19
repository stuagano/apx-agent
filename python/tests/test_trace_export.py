"""Tests for _trace_export.py — MLflow traces → Delta table.

Covers:
  - Target table path validation (must be three-part)
  - Auto-create DDL fires on first run
  - Trace normalisation pulls apx.* attrs off DataFrame rows and
    Trace objects
  - MERGE statement built from the normalised rows
  - Failures degrade to ExportResult with rows_written=0
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mlflow")

from apx_agent import export_traces  # noqa: E402
from apx_agent._trace_export import (  # noqa: E402
    _normalise_trace,
    ExportResult,
)


# ---------------------------------------------------------------------------
# _normalise_trace
# ---------------------------------------------------------------------------


def test_normalise_dataframe_row() -> None:
    row = {
        "trace_id": "tr-1",
        "experiment_id": "exp-1",
        "tags": {
            "apx.agent.name": "triage",
            "apx.operation": "predict",
            "apx.session.id": "user:alice:thread-1",
            "apx.user.token_provided": True,
            "apx.model.endpoint": "databricks-claude-sonnet-4-6",
            "apx.tools.count": 5,
            "apx.watchdog.action": "allow",
        },
        "status": "OK",
        "timestamp_ms": 1700000000000,
        "execution_time_ms": 1234,
    }
    norm = _normalise_trace(row)
    assert norm is not None
    assert norm["trace_id"] == "tr-1"
    assert norm["agent_name"] == "triage"
    assert norm["operation"] == "predict"
    assert norm["session_id"] == "user:alice:thread-1"
    assert norm["user_token_provided"] is True
    assert norm["model_endpoint"] == "databricks-claude-sonnet-4-6"
    assert norm["tool_count"] == 5
    assert norm["watchdog_action"] == "allow"


def test_normalise_trace_object() -> None:
    span = SimpleNamespace(attributes={
        "apx.agent.name": "billing",
        "apx.operation": "tool_call",
    })
    info = SimpleNamespace(
        trace_id="tr-2",
        experiment_id="exp-1",
        status="OK",
        timestamp_ms=1700000001000,
        execution_time_ms=42,
        tags={},
    )
    trace = SimpleNamespace(info=info, data=SimpleNamespace(spans=[span]))
    norm = _normalise_trace(trace)
    assert norm is not None
    assert norm["trace_id"] == "tr-2"
    assert norm["agent_name"] == "billing"
    assert norm["operation"] == "tool_call"


def test_normalise_returns_none_when_no_trace_id() -> None:
    assert _normalise_trace({"tags": {}}) is None
    assert _normalise_trace(SimpleNamespace(info=None)) is None


# ---------------------------------------------------------------------------
# export_traces
# ---------------------------------------------------------------------------


def test_export_traces_rejects_bad_table_path() -> None:
    with pytest.raises(ValueError, match="three-part"):
        export_traces(
            experiment_name="x",
            target_table="not.three",
            ws=MagicMock(),
        )


def test_export_traces_runs_create_table_when_auto_create() -> None:
    ws = MagicMock()
    fake_traces = SimpleNamespace(to_dict=lambda orient: [])

    with patch("mlflow.search_traces", return_value=fake_traces), \
         patch("apx_agent._trace_export.run_sql") as mock_sql:
        export_traces(
            experiment_name="exp",
            target_table="main.x.traces",
            ws=ws,
        )

    create_calls = [c for c in mock_sql.call_args_list if "CREATE TABLE IF NOT EXISTS" in c.args[1]]
    assert len(create_calls) == 1


def test_export_traces_skips_create_table_when_disabled() -> None:
    ws = MagicMock()
    fake_traces = SimpleNamespace(to_dict=lambda orient: [])

    with patch("mlflow.search_traces", return_value=fake_traces), \
         patch("apx_agent._trace_export.run_sql") as mock_sql:
        export_traces(
            experiment_name="exp",
            target_table="main.x.traces",
            ws=ws,
            auto_create=False,
        )

    create_calls = [c for c in mock_sql.call_args_list if "CREATE TABLE" in c.args[1]]
    assert create_calls == []


def test_export_traces_writes_merge_with_normalised_rows() -> None:
    ws = MagicMock()
    fake_traces = SimpleNamespace(to_dict=lambda orient: [
        {
            "trace_id": "tr-1",
            "tags": {"apx.agent.name": "triage", "apx.operation": "predict"},
            "status": "OK",
            "execution_time_ms": 100,
        },
        {
            "trace_id": "tr-2",
            "tags": {"apx.agent.name": "billing"},
            "status": "OK",
            "execution_time_ms": 50,
        },
    ])

    with patch("mlflow.search_traces", return_value=fake_traces), \
         patch("apx_agent._trace_export.run_sql") as mock_sql:
        result = export_traces(
            experiment_name="exp",
            target_table="main.x.traces",
            ws=ws,
            auto_create=False,
        )

    assert result.rows_written == 2
    assert result.skipped == 0
    merge_calls = [c for c in mock_sql.call_args_list if "MERGE INTO main.x.traces" in c.args[1]]
    assert len(merge_calls) == 1
    sql = merge_calls[0].args[1]
    assert "tr-1" in sql
    assert "tr-2" in sql
    assert "triage" in sql


def test_export_traces_returns_zero_written_on_sql_failure() -> None:
    ws = MagicMock()
    fake_traces = SimpleNamespace(to_dict=lambda orient: [
        {"trace_id": "tr-1", "tags": {}, "status": "OK"},
    ])

    call_count = {"n": 0}

    def _fail_on_merge(_ws, sql, **kwargs):
        call_count["n"] += 1
        if "MERGE" in sql:
            raise RuntimeError("merge denied")
        return []

    with patch("mlflow.search_traces", return_value=fake_traces), \
         patch("apx_agent._trace_export.run_sql", side_effect=_fail_on_merge):
        result = export_traces(
            experiment_name="exp",
            target_table="main.x.traces",
            ws=ws,
            auto_create=False,
        )

    assert result.rows_written == 0
    assert result.traces_pulled == 1


def test_export_traces_counts_unusable_traces_as_skipped() -> None:
    ws = MagicMock()
    fake_traces = SimpleNamespace(to_dict=lambda orient: [
        {"tags": {}},  # no trace_id — should skip
        {"trace_id": "tr-2", "tags": {"apx.agent.name": "x"}, "status": "OK"},
    ])

    with patch("mlflow.search_traces", return_value=fake_traces), \
         patch("apx_agent._trace_export.run_sql"):
        result = export_traces(
            experiment_name="exp",
            target_table="main.x.traces",
            ws=ws,
            auto_create=False,
        )

    assert result.skipped == 1
    assert result.rows_written == 1


def test_export_result_is_frozen() -> None:
    r = ExportResult(target_table="x.y.z", traces_pulled=1, rows_written=1, skipped=0)
    with pytest.raises(Exception):
        r.rows_written = 99  # type: ignore[misc]
