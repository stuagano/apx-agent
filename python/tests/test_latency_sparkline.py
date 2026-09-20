"""Chat landing p50/p95 latency sparkline (#748)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import _latency_trend, build_dev_ui_router


@pytest.fixture(autouse=True)
def _isolate_traces_cache() -> Any:
    """Do not leak last-20 durations into sibling traces tests."""
    from apx_agent._dev import _TRACES_LIST_CACHE

    _TRACES_LIST_CACHE.clear()
    yield
    _TRACES_LIST_CACHE.clear()


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(build_dev_ui_router())
    return a


def _row(tid: str, duration_ms: int | None) -> dict[str, Any]:
    return {
        "trace_id": tid,
        "state": "OK",
        "request_time_ms": 1,
        "duration_ms": duration_ms,
        "request_preview": "",
        "response_preview": "",
    }


class TestLatencyTrendHelper:
    def test_reverses_newest_first_and_computes_percentiles(self) -> None:
        # Newest-first list from search_traces(order_by=timestamp DESC).
        rows = [_row("n", 400), _row("mid", 200), _row("old", 100), _row("older", 300)]
        out = _latency_trend(rows)
        assert out["points"] == [300, 100, 200, 400]
        # _percentile nearest-rank on sorted [100, 200, 300, 400]
        assert out["p50_ms"] == 200
        assert out["p95_ms"] == 400
        assert out["n"] == 4

    def test_drops_missing_durations_and_caps_at_20(self) -> None:
        # 25 newest-first rows; first two have no duration (ring-buffer merge).
        rows = [_row("buf-1", None), _row("buf-2", None)]
        rows.extend(_row(f"t{i}", 1000 + i) for i in range(25))
        out = _latency_trend(rows)
        assert out["n"] == 20
        assert out["points"] == list(reversed([1000 + i for i in range(20)]))
        assert out["p50_ms"] == 1009
        assert out["p95_ms"] == 1018

    def test_empty_rows_return_none_percentiles(self) -> None:
        out = _latency_trend([])
        assert out == {"p50_ms": None, "p95_ms": None, "points": [], "n": 0}


class TestLatencyTrendRoute:
    @pytest.mark.asyncio
    async def test_returns_503_without_experiment(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/latency")
        assert r.status_code == 503
        assert r.json()["error"] == "MLFLOW_EXPERIMENT_ID not set"

    @pytest.mark.asyncio
    async def test_empty_cache_returns_empty_trend(self, app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        from apx_agent._dev import _TRACES_LIST_CACHE

        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-spark")
        _TRACES_LIST_CACHE.put([])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/latency")
        assert r.status_code == 200
        assert r.json() == {"p50_ms": None, "p95_ms": None, "points": [], "n": 0}

    @pytest.mark.asyncio
    async def test_cached_rows_drive_points_not_dummy_series(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apx_agent._dev import _TRACES_LIST_CACHE

        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-spark")
        # Distinct durations so a dummy 1..n series cannot match.
        newest_first = [77, 250, 91, 410, 88]
        _TRACES_LIST_CACHE.put([_row(f"t{i}", ms) for i, ms in enumerate(newest_first)])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/latency")
        body = r.json()
        assert r.status_code == 200
        assert body["points"] == [88, 410, 91, 250, 77]
        assert body["n"] == 5
        assert body["p50_ms"] == 88
        assert body["p95_ms"] == 410

    @pytest.mark.asyncio
    async def test_fetch_path_uses_trace_duration_ms(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "exp-spark")
        fetched = [_row("live", 1234), _row("older", 56)]

        async def _fake_fetch(experiment_id: str | None, max_results: int) -> list[dict[str, Any]]:
            assert experiment_id == "exp-spark"
            assert max_results == 50
            return fetched

        with patch("apx_agent._dev._fetch_traces_list_async", _fake_fetch):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                r = await ac.get("/_apx/traces/latency")
        assert r.status_code == 200
        assert r.json()["points"] == [56, 1234]
        assert r.json()["p50_ms"] == 56
        assert r.json()["p95_ms"] == 1234


class TestLandingSparklineMount:
    def test_landing_html_includes_sparkline_mount(self) -> None:
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentCard
        from apx_agent._ui_chat import _render_landing

        ctx = AgentContext(
            config=AgentConfig(name="demo-agent", model="claude-fake"),
            tools=[],
            card=AgentCard(name="demo-agent", description="", skills=[]),
            agent=None,  # type: ignore[arg-type]
        )
        html = _render_landing(ctx)
        assert 'id="latency-spark"' in html
        assert 'id="latency-spark-line"' in html
        assert 'id="latency-p50"' in html
        assert 'id="latency-p95"' in html
        # Schema-card tests treat "data-card" as the tables card. Sparkline
        # must not reuse that class or the no-schema landing fails CI.
        assert "data-card" not in html
        assert 'class="latency-spark-card"' in html

    def test_agent_ui_fetches_latency_route(self) -> None:
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentCard
        from apx_agent._ui_chat import _render_agent_ui

        ctx = AgentContext(
            config=AgentConfig(name="demo-agent", model="claude-fake"),
            tools=[],
            card=AgentCard(name="demo-agent", description="", skills=[]),
            agent=None,  # type: ignore[arg-type]
        )
        html = _render_agent_ui(ctx)
        assert "/_apx/traces/latency" in html
        assert "latency-spark-line" in html
