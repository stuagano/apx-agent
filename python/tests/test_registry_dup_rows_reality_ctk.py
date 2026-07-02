"""Claim-vs-reality: a failed DELETE must not leave a duplicate registry row (#357).

`publish_standalone_tools_to_registry` deletes a tool's existing standalone row
and then runs a **plain INSERT** (not a MERGE). The delete was wrapped in
`except Exception: pass`, so a transient warehouse error or permissions gap on
the DELETE was silently discarded and the INSERT still ran — adding a second
`(name, agent_id IS NULL)` row. Every republish after that multiplied the rows,
and no error told the operator.

A "publish returned N" test can't see this. This reconciles the *claim* ("the
tool is upserted") against reality: when the DELETE fails, the INSERT must NOT
run (no duplicate), the tool must be reported skipped, and the operator must be
told — while a clean DELETE still inserts as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apx_agent._publish import publish_standalone_tools_to_registry


@dataclass
class _PublishRun:
    count: int
    calls: list[str]


def _tool() -> str:
    """A standalone tool."""
    return "x"


_tool.__name__ = "my_tool"


def _run(delete_fails: bool, caplog) -> _PublishRun:
    calls: list[str] = []

    def _fake_run_sql(_ws: Any, sql: str, warehouse_id: Any = None, **_kw: Any) -> list:
        calls.append(sql)
        if delete_fails and "DELETE" in sql:
            raise RuntimeError("warehouse transient error")
        return []

    with patch("apx_agent._sql.run_sql", side_effect=_fake_run_sql), caplog.at_level(
        logging.ERROR
    ):
        count = publish_standalone_tools_to_registry(
            tool_fns=[_tool],
            tools_table="main.apx.agent_tools",
            ws=MagicMock(name="ws"),
            warehouse_id="wh1",
        )
    return _PublishRun(count, calls)


@pytest.mark.unit
def test_failed_delete_does_not_insert_duplicate(caplog) -> None:
    run = _run(delete_fails=True, caplog=caplog)
    inserts = [s for s in run.calls if "INSERT INTO" in s]
    assert inserts == [], f"INSERT ran after a failed DELETE → duplicate row: {inserts}"
    assert run.count == 0
    assert "my_tool" in caplog.text, "operator not told which tool was skipped"


@pytest.mark.unit
def test_clean_delete_still_inserts(caplog) -> None:
    run = _run(delete_fails=False, caplog=caplog)
    inserts = [s for s in run.calls if "INSERT INTO" in s]
    assert len(inserts) == 1, f"happy-path insert regressed: {inserts}"
    assert run.count == 1
