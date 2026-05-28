"""Tests for SQL warehouse cold-start pre-warm in _ensure_warehouse_running.

The serverless starter auto-stops after its idle window; the next query against
a stopped warehouse would queue inside execute_statement and surface as
"Query failed" after the wait_timeout. _ensure_warehouse_running converts
that silent cold-start into a logged pre-warm + poll-to-RUNNING.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("databricks.sdk")

from databricks.sdk.service.sql import State  # noqa: E402

from apx_agent._sql import _ensure_warehouse_running  # noqa: E402


def test_noop_when_warehouse_running() -> None:
    """A RUNNING warehouse → no start, no poll."""
    ws = MagicMock()
    ws.warehouses.get.return_value = MagicMock(state=State.RUNNING)
    _ensure_warehouse_running(ws, "wh-1")
    ws.warehouses.start.assert_not_called()
    assert ws.warehouses.get.call_count == 1


def test_starts_stopped_warehouse_and_polls_to_running() -> None:
    """STOPPED → start + poll until RUNNING; sleep is mocked for speed."""
    ws = MagicMock()
    ws.warehouses.get.side_effect = [
        MagicMock(state=State.STOPPED),   # initial check
        MagicMock(state=State.STARTING),  # first poll
        MagicMock(state=State.RUNNING),   # second poll → done
    ]
    with patch("apx_agent._sql.time") as mt:
        # monotonic must advance so the deadline loop terminates if needed.
        mt.monotonic.side_effect = [0, 1, 3, 5]
        mt.sleep = MagicMock()
        _ensure_warehouse_running(ws, "wh-2", timeout_s=30)
    ws.warehouses.start.assert_called_once_with("wh-2")
    assert ws.warehouses.get.call_count == 3


def test_swallows_get_failure_so_query_still_runs() -> None:
    """If the warmup check itself errors, we don't block the query — let
    execute_statement try; it's authoritative."""
    ws = MagicMock()
    ws.warehouses.get.side_effect = Exception("perm denied")
    _ensure_warehouse_running(ws, "wh-3")  # must not raise
    ws.warehouses.start.assert_not_called()
