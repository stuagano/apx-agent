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
        # Strict ReplayToolRequest: a missing required field is rejected by
        # FastAPI with 422 before the handler runs (policy #1, was a typed 400).
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={"args": {"city": "x"}})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_422_when_args_not_a_dict(self, app_with_tool: FastAPI):
        # The envelope is strict even though args' *contents* are permissive:
        # args must be an object, not a scalar.
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={"tool_name": "get_weather", "args": "nope"})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_args_optional_defaults_to_empty(self, app_with_tool: FastAPI):
        # args is permissive AND optional — omitting it defaults to {} (the tool
        # then validates its own required params and reports ok:false at 200).
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/tool", json={"tool_name": "get_weather"})
        assert r.status_code == 200
        assert r.json()["ok"] is False

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
    async def test_returns_422_for_empty_messages(self, app_with_tool: FastAPI):
        # min_length=1 on ReplayLlmRequest.messages: an empty list is a shape
        # error → 422 (policy #1, was a typed 400).
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={"messages": []})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_422_when_messages_missing(self, app_with_tool: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_422_when_messages_not_a_list(self, app_with_tool: FastAPI):
        # Envelope is strict though message *contents* are permissive dicts.
        async with AsyncClient(transport=ASGITransport(app=app_with_tool), base_url="http://test") as ac:
            r = await ac.post("/_apx/replay/llm", json={"messages": "hi"})
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


class TestReplayHiddenFromSchema:
    """#281's defining constraint: the replay pair is typed but KEPT HIDDEN.

    They execute arbitrary tools with forwarded OBO creds / invoke the LLM
    directly, so — unlike the rest of the un-hidden dev-UI surface — their shape
    must not appear in the generated OpenAPI/Scalar schema even for an
    authenticated user. The bodies are still validated (the tests above prove
    the 422s), but the contract here is *absence* from the schema.
    """

    @pytest.mark.asyncio
    async def test_replay_routes_absent_from_openapi(self, app_with_tool: FastAPI):
        paths = app_with_tool.openapi()["paths"]
        assert "/_apx/replay/tool" not in paths
        assert "/_apx/replay/llm" not in paths

    @pytest.mark.asyncio
    async def test_an_unhidden_route_is_present_for_contrast(self, app_with_tool: FastAPI):
        # Guard against a vacuous pass: the schema is populated, and a route that
        # IS meant to be visible shows up — only replay/* is filtered out.
        paths = app_with_tool.openapi()["paths"]
        assert paths, "expected a non-empty OpenAPI schema"
        assert any(p.startswith("/_apx/") for p in paths)
