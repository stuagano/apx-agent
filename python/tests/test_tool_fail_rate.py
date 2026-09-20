"""Per-tool fail rate on the traces list (#749)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import _render_traces_list, _tool_fail_rates, build_dev_ui_router
from apx_agent import _trace_store


@pytest.fixture(autouse=True)
def _isolate_trace_store() -> Any:
    """Do not leak ring-buffer spans into sibling traces tests."""
    _trace_store.reset()
    yield
    _trace_store.reset()


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(build_dev_ui_router())
    return a


def _span(name: str, span_type: str, status: str) -> dict[str, Any]:
    return {"name": name, "span_type": span_type, "status": status}


class TestToolFailRatesHelper:
    def test_counts_tool_not_llm_and_error_not_ok(self) -> None:
        out = _tool_fail_rates(
            [
                [
                    _span("sql_tool", "TOOL", "ERROR"),
                    _span("sql_tool", "TOOL", "OK"),
                    _span("chat", "LLM", "ERROR"),
                    _span("planner", "AGENT", "ERROR"),
                ]
            ]
        )
        assert out["n_traces"] == 1
        assert out["tools"] == [{"name": "sql_tool", "failed": 1, "total": 2}]

    def test_retriever_counts_and_status_code_error(self) -> None:
        out = _tool_fail_rates(
            [
                [
                    _span("kb", "RETRIEVER", "STATUS_CODE_ERROR"),
                    _span("kb", "RETRIEVER", "OK"),
                    _span("kb", "RETRIEVER", "FAIL"),
                ]
            ]
        )
        assert out["tools"] == [{"name": "kb", "failed": 2, "total": 3}]

    def test_sorts_by_failed_desc_then_name(self) -> None:
        out = _tool_fail_rates(
            [
                [_span("zeta", "TOOL", "ERROR"), _span("alpha", "TOOL", "ERROR")],
                [_span("zeta", "TOOL", "ERROR"), _span("beta", "TOOL", "OK")],
            ]
        )
        assert [row["name"] for row in out["tools"]] == ["zeta", "alpha", "beta"]
        assert out["tools"][0] == {"name": "zeta", "failed": 2, "total": 2}
        assert out["n_traces"] == 2

    def test_empty_and_nameless_are_dropped(self) -> None:
        out = _tool_fail_rates([])
        assert out == {"tools": [], "n_traces": 0}
        skipped = _tool_fail_rates([[_span("", "TOOL", "ERROR"), {"span_type": "TOOL", "status": "ERROR"}]])
        assert skipped == {"tools": [], "n_traces": 1}


class TestToolFailRateRoute:
    @pytest.mark.asyncio
    async def test_empty_buffer_returns_empty(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/tool-fails")
        assert r.status_code == 200
        assert r.json() == {"tools": [], "n_traces": 0}

    @pytest.mark.asyncio
    async def test_mixed_statuses_from_ring_buffer(self, app: FastAPI) -> None:
        _trace_store.put(
            "t1",
            [
                _span("sql_tool", "TOOL", "ERROR"),
                _span("sql_tool", "TOOL", "OK"),
                _span("sql_tool", "TOOL", "OK"),
                _span("search", "TOOL", "OK"),
                _span("think", "LLM", "ERROR"),
            ],
        )
        _trace_store.put(
            "t2",
            [
                _span("sql_tool", "TOOL", "STATUS_CODE_ERROR"),
                _span("search", "RETRIEVER", "FAIL"),
            ],
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/tool-fails")
        body = r.json()
        assert r.status_code == 200
        assert body["n_traces"] == 2
        assert body["tools"] == [
            {"name": "sql_tool", "failed": 2, "total": 4},
            {"name": "search", "failed": 1, "total": 2},
        ]

    @pytest.mark.asyncio
    async def test_static_path_is_not_a_trace_id(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/tool-fails")
        assert r.status_code == 200
        assert "tools" in r.json()


class TestTracesListMount:
    def test_html_includes_mount_and_fetch(self) -> None:
        html = _render_traces_list(None, "demo")
        assert 'id="tool-fails"' in html
        assert 'class="tool-fails-card"' in html
        assert "/_apx/traces/tool-fails" in html
        assert "data-card" not in html
