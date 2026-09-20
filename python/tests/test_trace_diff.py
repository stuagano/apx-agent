"""Side-by-side trace diff (#750)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import _trace_store
from apx_agent._dev import (
    _render_trace_diff,
    _render_traces_list,
    _trace_diff_side,
    build_dev_ui_router,
)


@pytest.fixture(autouse=True)
def _isolate_trace_store() -> Any:
    _trace_store.reset()
    yield
    _trace_store.reset()


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(build_dev_ui_router())
    return a


def _user_span(question: str) -> dict[str, Any]:
    return {
        "name": "chat",
        "span_type": "LLM",
        "status": "OK",
        "inputs": {"messages": [{"role": "user", "content": question}]},
        "outputs": {"choices": [{"message": {"role": "assistant", "content": "ignored-first"}}]},
    }


def _assistant_span(answer: str) -> dict[str, Any]:
    return {
        "name": "chat",
        "span_type": "LLM",
        "status": "OK",
        "inputs": {"messages": [{"role": "assistant", "content": "prior"}]},
        "outputs": {"choices": [{"message": {"role": "assistant", "content": answer}}]},
    }


class TestTraceDiffHelper:
    def test_extracts_question_and_last_response(self) -> None:
        out = _trace_diff_side(
            "t1",
            [_user_span("What is 2+2?"), _assistant_span("4")],
            [],
        )
        assert out["trace_id"] == "t1"
        assert out["found"] is True
        assert out["question"] == "What is 2+2?"
        assert out["response"] == "4"
        assert out["judge_verdict"] is None

    def test_judge_from_eval_row_only(self) -> None:
        out = _trace_diff_side(
            "t1",
            [_user_span("q"), _assistant_span("a")],
            [{"trace_id": "t1", "judge_verdict": "PASS", "judge_reason": "matches"}],
        )
        assert out["judge_verdict"] == "PASS"
        assert out["judge_reason"] == "matches"

    def test_missing_buffer_uses_eval_row_and_found_false(self) -> None:
        out = _trace_diff_side(
            "gone",
            None,
            [{"trace_id": "gone", "question": "cached q", "response": "cached a", "judge_verdict": "FAIL"}],
        )
        assert out["found"] is False
        assert out["question"] == "cached q"
        assert out["response"] == "cached a"
        assert out["judge_verdict"] == "FAIL"

    def test_other_trace_eval_row_is_ignored(self) -> None:
        out = _trace_diff_side("t1", [_user_span("q")], [{"trace_id": "other", "judge_verdict": "PASS"}])
        assert out["judge_verdict"] is None


class TestTraceDiffRoute:
    @pytest.mark.asyncio
    async def test_missing_ids_return_400_json(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/diff?fmt=json")
        assert r.status_code == 400
        assert "a and b" in r.json()["error"]

    @pytest.mark.asyncio
    async def test_compares_two_buffered_traces(self, app: FastAPI) -> None:
        _trace_store.put("left", [_user_span("same q"), _assistant_span("old")])
        _trace_store.put("right", [_user_span("same q"), _assistant_span("new")])
        rows = [
            {"trace_id": "left", "judge_verdict": "FAIL", "judge_reason": "stale"},
            {"trace_id": "right", "judge_verdict": "PASS", "judge_reason": "fixed"},
        ]
        with patch("apx_agent._dev._load_optimize_eval_rows", return_value=rows):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
                r = await ac.get("/_apx/traces/diff?a=left&b=right&fmt=json")
        body = r.json()
        assert r.status_code == 200
        assert body["left"]["response"] == "old"
        assert body["right"]["response"] == "new"
        assert body["left"]["judge_verdict"] == "FAIL"
        assert body["right"]["judge_verdict"] == "PASS"

    @pytest.mark.asyncio
    async def test_static_path_is_not_a_trace_id(self, app: FastAPI) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
            r = await ac.get("/_apx/traces/diff?fmt=json")
        assert r.status_code == 400


class TestTraceDiffMount:
    def test_list_html_has_compare_bar(self) -> None:
        html = _render_traces_list(None, "demo")
        assert 'id="trace-diff-bar"' in html
        assert 'id="trace-diff-go"' in html
        assert "/_apx/traces/diff" in html
        assert "data-card" not in html

    def test_diff_html_uses_own_card_class(self) -> None:
        html = _render_trace_diff(
            {
                "trace_id": "left",
                "found": True,
                "question": "q",
                "response": "old",
                "judge_verdict": "FAIL",
                "judge_reason": None,
            },
            {
                "trace_id": "right",
                "found": True,
                "question": "q",
                "response": "new",
                "judge_verdict": "PASS",
                "judge_reason": None,
            },
        )
        assert "trace-diff-card" in html
        assert "data-card" not in html
        assert "Same question" in html
        assert "old" in html
        assert "new" in html
