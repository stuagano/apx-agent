"""component_id is an LLM tool argument: it must reach SQL as a bound
parameter, never interpolated into the statement text."""

from __future__ import annotations

from typing import Any

import apx_agent
import pytest

from scripts import compile_demo

_HOSTILE = "X' OR '1'='1"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run_sql(ws: Any, sql: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"sql": sql, **kwargs})
        return []

    monkeypatch.setattr(apx_agent, "run_sql", fake_run_sql)
    monkeypatch.setenv("HISTORICAL_DEMAND_TABLE", "cat.sch.history")
    monkeypatch.setenv("PARTS_CATALOG_TABLE", "cat.sch.parts")
    return calls


def _assert_bound(call: dict[str, Any]) -> None:
    assert _HOSTILE not in call["sql"]
    assert ":component_id" in call["sql"]
    assert call["parameters"] == [
        {"name": "component_id", "value": _HOSTILE, "type": "STRING"},
    ]


def test_find_historical_patterns_binds_component_id(captured: list[dict[str, Any]]) -> None:
    compile_demo.find_historical_patterns(_HOSTILE, 5, ws=None)
    _assert_bound(captured[0])


def test_find_alternative_parts_binds_component_id(captured: list[dict[str, Any]]) -> None:
    compile_demo.find_alternative_parts(_HOSTILE, 5, ws=None)
    _assert_bound(captured[0])
