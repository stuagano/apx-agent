"""Claim-vs-reality: a partial tool publish must fail loudly (#382).

publish_tools_to_registry DELETEs all rows for an agent then re-inserts each tool
via _insert_tool_row, which swallows per-row failures (returns 0). So a transient
SQL error on one tool left the registry advertising FEWER tools than the deployed
agent has, while the function returned a (lower) count and logged success — the
drift was invisible.

This reconciles the *claim* ("the registry reflects the deployed tool set")
against reality: when fewer rows are written than tools requested, the publish
raises instead of silently returning a short count.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import apx_agent._publish as pub
from apx_agent._publish import publish_tools_to_registry


def _t1() -> str:
    """Tool one."""
    return "1"


def _t2() -> str:
    """Tool two."""
    return "2"


@pytest.mark.unit
def test_partial_insert_failure_raises_not_short_count(monkeypatch) -> None:
    # Schema CREATE + DELETE succeed; the per-tool inserts are stubbed so the
    # second one "fails" (returns 0), as _insert_tool_row does when it swallows.
    monkeypatch.setattr(pub, "run_sql", lambda *_a, **_k: [], raising=False)
    import apx_agent._sql as _sql

    monkeypatch.setattr(_sql, "run_sql", lambda *_a, **_k: [])

    results = iter([1, 0])  # tool 1 inserts, tool 2 fails
    monkeypatch.setattr(pub, "_insert_tool_row", lambda **_k: next(results))

    with pytest.raises(RuntimeError, match="incomplete|1/2"):
        publish_tools_to_registry(
            agent_id="a1",
            agent_name="Agent One",
            tool_fns=[_t1, _t2],
            ws=MagicMock(name="ws"),
            warehouse_id="wh",
        )


@pytest.mark.unit
def test_all_inserts_succeed_returns_count(monkeypatch) -> None:
    import apx_agent._sql as _sql

    monkeypatch.setattr(_sql, "run_sql", lambda *_a, **_k: [])
    monkeypatch.setattr(pub, "_insert_tool_row", lambda **_k: 1)

    n = publish_tools_to_registry(
        agent_id="a1",
        agent_name="Agent One",
        tool_fns=[_t1, _t2],
        ws=MagicMock(name="ws"),
        warehouse_id="wh",
    )
    assert n == 2
