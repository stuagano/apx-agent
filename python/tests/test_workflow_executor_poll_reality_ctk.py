"""Claim-vs-reality: workflow SQL executors must poll slow statements (#372).

DeltaEngine._exec_sync and PopulationStore._sql_exec ran
``execute_statement(wait_timeout="50s")`` and treated only FAILED as an error;
a statement still RUNNING/PENDING after 50s fell through to ``return []``. So a
slow read looked empty and a slow write reported success though rows never
landed — silent data loss in the evolutionary loop.

This reconciles the *claim* ("the query ran; here are its rows") against reality:
when execute_statement returns RUNNING, the executor must poll to completion and
return the actual rows — not [] — and must raise (not return []) on a
non-SUCCEEDED terminal state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent.workflow import _sql_exec
from apx_agent.workflow._sql_exec import execute_and_poll


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sql_exec.time, "sleep", lambda *_a: None)


def _status(state: Any, error: Any = None) -> Any:
    return SimpleNamespace(state=state, error=error)


def _rows_resp(state: Any, rows: list[list[Any]] | None) -> Any:
    from databricks.sdk.service.sql import StatementState  # noqa: F401 — ensure importable

    manifest = SimpleNamespace(
        schema=SimpleNamespace(columns=[SimpleNamespace(name="c")])
    )
    result = SimpleNamespace(data_array=rows) if rows is not None else None
    return SimpleNamespace(status=_status(state), statement_id="s1", result=result, manifest=manifest)


def _ws_running_then(final_resp: Any) -> Any:
    """A ws whose execute_statement returns RUNNING, then get_statement returns final_resp."""
    from databricks.sdk.service.sql import StatementState

    running = SimpleNamespace(
        status=_status(StatementState.RUNNING), statement_id="s1", result=None, manifest=None
    )
    ws = MagicMock(name="ws")
    ws.statement_execution.execute_statement.return_value = running
    ws.statement_execution.get_statement.return_value = final_resp
    return ws


@pytest.mark.unit
def test_running_statement_is_polled_not_returned_empty() -> None:
    from databricks.sdk.service.sql import StatementState

    ws = _ws_running_then(_rows_resp(StatementState.SUCCEEDED, [["v"]]))
    rows = execute_and_poll(ws, "wh", "SELECT c FROM t", poll_interval_s=0)
    assert rows == [{"c": "v"}], "slow statement returned empty instead of its polled rows"
    ws.statement_execution.get_statement.assert_called()  # it actually polled


@pytest.mark.unit
def test_slow_statement_that_fails_raises_not_empty() -> None:
    from databricks.sdk.service.sql import StatementState

    failed = SimpleNamespace(
        status=_status(StatementState.FAILED, SimpleNamespace(error_code="X", message="boom")),
        statement_id="s1",
        result=None,
        manifest=None,
    )
    ws = _ws_running_then(failed)
    with pytest.raises(RuntimeError, match="boom|FAILED"):
        execute_and_poll(ws, "wh", "INSERT ...", poll_interval_s=0)


@pytest.mark.unit
def test_both_workflow_executors_route_through_execute_and_poll(monkeypatch) -> None:
    # Prove the fix is wired at both call sites, not just that the helper exists.
    calls: list[str] = []

    def _recorder(_ws: Any, _wh: Any, sql: str, **_k: Any) -> list[dict]:
        calls.append(sql)
        return []

    from apx_agent.workflow import engine_delta, population_store

    monkeypatch.setattr(engine_delta, "execute_and_poll", _recorder)
    monkeypatch.setattr(population_store, "execute_and_poll", _recorder)

    engine_delta.DeltaEngine(
        ws=MagicMock(), table_prefix="p", warehouse_id="wh"
    )._exec_sync("SELECT 1")
    population_store.PopulationStore(
        ws=MagicMock(), config=SimpleNamespace(warehouse_id="wh")
    )._sql_exec("SELECT 2")

    assert "SELECT 1" in calls and "SELECT 2" in calls, "a call site bypassed execute_and_poll"
