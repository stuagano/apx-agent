"""Tests for /_apx/eval/judge — LLM-as-judge scoring endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import Agent, AgentConfig, setup_agent
from apx_agent._dev import _parse_judge_output, build_dev_ui_router

from .conftest import get_weather


@pytest.fixture
async def app_with_model() -> FastAPI:
    app = FastAPI()
    agent = Agent(tools=[get_weather])
    config = AgentConfig(name="judge-test", model="claude-fake")
    await setup_agent(app, agent, config)
    app.include_router(build_dev_ui_router())
    return app


def _patch_judge_output(text: str):
    sdk = AsyncMock()
    sdk.responses.create = AsyncMock(return_value=MagicMock(output_text=text))
    return patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk), sdk


class TestParseJudgeOutput:
    def test_strict_format_pass(self):
        v, r = _parse_judge_output("VERDICT: PASS\nREASON: Looks correct.")
        assert v == "PASS"
        assert r == "Looks correct."

    def test_strict_format_fail(self):
        v, r = _parse_judge_output("VERDICT: FAIL\nREASON: Wrong city.")
        assert v == "FAIL"
        assert r == "Wrong city."

    def test_unlabeled_pass_inferred(self):
        v, r = _parse_judge_output("PASS — this answer is clearly correct")
        assert v == "PASS"
        assert "PASS" in r

    def test_ambiguous_defaults_to_fail(self):
        v, _ = _parse_judge_output("I think this is mostly fine.")
        assert v == "FAIL"

    def test_empty_defaults_to_fail(self):
        v, r = _parse_judge_output("")
        assert v == "FAIL"
        assert "judge" in r.lower()

    def test_explicit_fail_word_does_not_become_pass(self):
        v, _ = _parse_judge_output("This response would FAIL the criterion.")
        assert v == "FAIL"


class TestEvalJudgeRoute:
    @pytest.mark.asyncio
    async def test_pass_verdict(self, app_with_model: FastAPI):
        patch_ctx, sdk = _patch_judge_output("VERDICT: PASS\nREASON: Correct answer.")
        with patch_ctx:
            async with AsyncClient(transport=ASGITransport(app=app_with_model), base_url="http://test") as ac:
                r = await ac.post("/_apx/eval/judge", json={
                    "question": "What's 2+2?",
                    "response": "4",
                    "criterion": "answer is the integer 4",
                })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["pass"] is True
        assert data["verdict"] == "PASS"
        assert data["reason"] == "Correct answer."
        assert data["model"] == "claude-fake"

    @pytest.mark.asyncio
    async def test_fail_verdict(self, app_with_model: FastAPI):
        patch_ctx, _ = _patch_judge_output("VERDICT: FAIL\nREASON: Off-topic.")
        with patch_ctx:
            async with AsyncClient(transport=ASGITransport(app=app_with_model), base_url="http://test") as ac:
                r = await ac.post("/_apx/eval/judge", json={
                    "question": "Q?",
                    "response": "unrelated",
                    "criterion": "answers the question",
                })
        data = r.json()
        assert data["ok"] is True
        assert data["pass"] is False
        assert data["verdict"] == "FAIL"

    @pytest.mark.asyncio
    async def test_ambiguous_judge_output_fails(self, app_with_model: FastAPI):
        patch_ctx, _ = _patch_judge_output("Hmm, hard to say.")
        with patch_ctx:
            async with AsyncClient(transport=ASGITransport(app=app_with_model), base_url="http://test") as ac:
                r = await ac.post("/_apx/eval/judge", json={
                    "question": "Q?", "response": "x", "criterion": "good answer",
                })
        assert r.json()["pass"] is False

    @pytest.mark.asyncio
    async def test_model_override(self, app_with_model: FastAPI):
        patch_ctx, sdk = _patch_judge_output("VERDICT: PASS\nREASON: ok")
        with patch_ctx:
            async with AsyncClient(transport=ASGITransport(app=app_with_model), base_url="http://test") as ac:
                r = await ac.post("/_apx/eval/judge", json={
                    "question": "Q?", "response": "ok", "criterion": "ok",
                    "model": "claude-judge-fake",
                })
        assert r.json()["model"] == "claude-judge-fake"
        assert sdk.responses.create.call_args.kwargs["model"] == "claude-judge-fake"

    @pytest.mark.asyncio
    async def test_model_exception_returns_ok_false(self, app_with_model: FastAPI):
        sdk = AsyncMock()
        sdk.responses.create = AsyncMock(side_effect=Exception("rate limited"))
        with patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk):
            async with AsyncClient(transport=ASGITransport(app=app_with_model), base_url="http://test") as ac:
                r = await ac.post("/_apx/eval/judge", json={
                    "question": "Q?", "response": "x", "criterion": "y",
                })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "rate limited" in data["error"]

    @pytest.mark.asyncio
    async def test_returns_400_when_fields_missing(self, app_with_model: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app_with_model), base_url="http://test") as ac:
            r = await ac.post("/_apx/eval/judge", json={"question": "Q?", "response": "x"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_400_when_no_model(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="no-model", model="")
        await setup_agent(app, agent, config)
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/eval/judge", json={
                "question": "Q?", "response": "x", "criterion": "y",
            })
        assert r.status_code == 400
        assert "model" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_503_when_no_context(self):
        app = FastAPI()
        app.state.agent_context = None
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/eval/judge", json={
                "question": "Q?", "response": "x", "criterion": "y",
            })
        assert r.status_code == 503
