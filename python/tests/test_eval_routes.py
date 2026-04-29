"""Tests for /_apx/eval/data persistence routes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import build_dev_ui_router


@pytest.fixture
def evals_path(tmp_path: Path) -> Path:
    """Patch _find_evals_path so the routes use a per-test temp file."""
    target = tmp_path / "evals.json"
    with patch("apx_agent._dev._find_evals_path", return_value=target):
        yield target


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(build_dev_ui_router())
    return a


class TestEvalDataGet:
    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_file(self, app: FastAPI, evals_path: Path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/_apx/eval/data")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_returns_persisted_cases(self, app: FastAPI, evals_path: Path):
        cases = [
            {"question": "what is 2+2?", "expected": "4", "status": "pass", "response": "4", "trace_id": "tr-1"},
        ]
        evals_path.write_text(json.dumps(cases))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/_apx/eval/data")
        assert r.status_code == 200
        assert r.json() == cases

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_agent_router(self, app: FastAPI):
        with patch("apx_agent._dev._find_evals_path", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.get("/_apx/eval/data")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_returns_500_on_corrupt_file(self, app: FastAPI, evals_path: Path):
        evals_path.write_text("not json {{{")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.get("/_apx/eval/data")
        assert r.status_code == 500


class TestEvalDataPost:
    @pytest.mark.asyncio
    async def test_writes_cases_to_disk(self, app: FastAPI, evals_path: Path):
        cases = [{"question": "hi", "expected": "", "status": "pending", "response": ""}]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/eval/data", json=cases)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "count": 1}
        assert json.loads(evals_path.read_text()) == cases

    @pytest.mark.asyncio
    async def test_round_trip_get_post(self, app: FastAPI, evals_path: Path):
        cases = [
            {"question": "q1", "expected": "a", "status": "pass", "response": "a", "trace_id": "tr-99"},
            {"question": "q2", "expected": "", "status": "fail", "response": "no", "trace_id": None},
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            assert (await ac.post("/_apx/eval/data", json=cases)).status_code == 200
            r = await ac.get("/_apx/eval/data")
        assert r.json() == cases

    @pytest.mark.asyncio
    async def test_rejects_non_list_body(self, app: FastAPI, evals_path: Path):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/eval/data", json={"not": "a list"})
        assert r.status_code == 400
        assert evals_path.exists() is False

    @pytest.mark.asyncio
    async def test_returns_503_when_no_agent_router(self, app: FastAPI):
        with patch("apx_agent._dev._find_evals_path", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.post("/_apx/eval/data", json=[])
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_overwrites_existing_file(self, app: FastAPI, evals_path: Path):
        evals_path.write_text(json.dumps([{"question": "old"}]))
        new_cases = [{"question": "new"}]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/eval/data", json=new_cases)
        assert r.status_code == 200
        assert json.loads(evals_path.read_text()) == new_cases
