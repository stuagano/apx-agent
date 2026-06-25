"""Tests for /_apx/replay/{tool,llm} — span replay endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import LlmAgent, AgentConfig, setup_agent
from apx_agent._dev import build_dev_ui_router

from .conftest import get_weather


@pytest.fixture
async def app_with_tool() -> FastAPI:
    app = FastAPI()
    agent = LlmAgent(tools=[get_weather])
    config = AgentConfig(name="replay-test", model="claude-fake")
    await setup_agent(app, agent, config)
    app.include_router(build_dev_ui_router())
    return app


class TestReplayTool:
    @pytest.mark.asyncio
    async def test_replays_tool_with_args(self, app_with_tool: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={
                "tool_name": "get_weather",
                "args": {"city": "Seattle", "country_code": "US"},
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "Seattle" in data["output"]
        assert isinstance(data["duration_ms"], int)

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_tool(self, app_with_tool: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={
                "tool_name": "nonexistent_tool",
                "args": {},
            })
        assert r.status_code == 404
        assert "not found" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_returns_422_when_tool_name_missing(self, app_with_tool: FastAPI):
        # PR-P1: tool_name is now a required model field, so a missing one is a
        # FastAPI 422 at the boundary (was a handler 400).
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={"args": {"city": "x"}})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_replay_routes_stay_hidden_from_openapi(self, app_with_tool: FastAPI):
        # The one exception to un-hide-everything: typed for validation, but NOT
        # advertised in OpenAPI (OBO arbitrary tool exec + direct LLM invoke).
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            paths = (await ac.get("/openapi.json")).json()["paths"]
        assert "/_apx/replay/tool" not in paths
        assert "/_apx/replay/llm" not in paths

    @pytest.mark.asyncio
    async def test_returns_503_when_no_agent_context(self):
        app = FastAPI()
        app.state.agent_context = None
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={"tool_name": "anything", "args": {}})
        assert r.status_code == 503

    @pytest.mark.asyncio
    async def test_tool_validation_error_returned_as_ok_false(self, app_with_tool: FastAPI):
        # get_weather requires `city`; missing it triggers validation 422 from FastAPI.
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={
                "tool_name": "get_weather",
                "args": {},  # missing required `city`
            })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert data["error"]


class TestReplayLlm:
    @pytest.mark.asyncio
    async def test_replays_with_default_model(self, app_with_tool: FastAPI):
        sdk = AsyncMock()
        sdk.chat.completions.create = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content="replayed answer"))]
            )
        )
        with patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk):
            async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
                r = await ac.post("/_apx/replay/llm", json={
                    "messages": [{"role": "user", "content": "what is 6*7?"}],
                })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["output"] == "replayed answer"
        assert data["model"] == "claude-fake"
        # Confirm the model received the edited messages
        call_kwargs = sdk.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-fake"
        assert call_kwargs["messages"] == [{"role": "user", "content": "what is 6*7?"}]

    @pytest.mark.asyncio
    async def test_model_override_in_body(self, app_with_tool: FastAPI):
        sdk = AsyncMock()
        sdk.chat.completions.create = AsyncMock(
            return_value=MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])
        )
        with patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk):
            async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
                r = await ac.post("/_apx/replay/llm", json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "claude-other",
                })
        assert r.status_code == 200
        assert r.json()["model"] == "claude-other"
        assert sdk.chat.completions.create.call_args.kwargs["model"] == "claude-other"

    @pytest.mark.asyncio
    async def test_returns_400_for_empty_messages(self, app_with_tool: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={"messages": []})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_422_when_messages_missing(self, app_with_tool: FastAPI):
        # PR-P1: messages is a required model field → missing is a 422 (was 400).
        # An empty list [] still reaches the handler's own 400 (separate test).
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_400_when_no_model_configured_or_passed(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="no-model", model="")
        await setup_agent(app, agent, config)
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={
                "messages": [{"role": "user", "content": "x"}],
            })
        assert r.status_code == 400
        assert "model" in r.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_model_exception_returned_as_ok_false(self, app_with_tool: FastAPI):
        sdk = AsyncMock()
        sdk.chat.completions.create = AsyncMock(side_effect=Exception("upstream timeout"))
        with patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk):
            async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
                r = await ac.post("/_apx/replay/llm", json={
                    "messages": [{"role": "user", "content": "hi"}],
                })
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "upstream timeout" in data["error"]

    @pytest.mark.asyncio
    async def test_returns_503_when_no_agent_context(self):
        app = FastAPI()
        app.state.agent_context = None
        app.include_router(build_dev_ui_router())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={
                "messages": [{"role": "user", "content": "x"}],
            })
        assert r.status_code == 503
