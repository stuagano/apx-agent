"""Tests for _wiring.py — setup_agent(), create_app(), and protocol routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import Agent, AgentConfig, AgentContext, create_app, setup_agent
from apx_agent._wiring import _mount_protocol_routes

from .conftest import get_weather, query_genie


# ---------------------------------------------------------------------------
# setup_agent
# ---------------------------------------------------------------------------


class TestSetupAgent:
    @pytest.mark.asyncio
    async def test_wires_protocol_routes(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="test-agent", description="Test")

        ctx = await setup_agent(app, agent, config)
        assert ctx is not None
        assert ctx.config.name == "test-agent"
        assert hasattr(app.state, "agent_context")
        assert app.state.agent_context is ctx

        # Check protocol routes exist
        route_paths = [r.path for r in app.routes]
        assert "/.well-known/agent.json" in route_paths
        assert "/responses" in route_paths
        assert "/health" in route_paths

    @pytest.mark.asyncio
    async def test_mounts_tool_routes(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="test-agent", api_prefix="/api")

        await setup_agent(app, agent, config)
        route_paths = [r.path for r in app.routes]
        assert "/api/tools/get_weather" in route_paths

    @pytest.mark.asyncio
    async def test_returns_none_when_no_config(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather])

        with patch("apx_agent._wiring._load_agent_config", return_value=None):
            ctx = await setup_agent(app, agent, config=None)
        assert ctx is None
        assert app.state.agent_context is None

    @pytest.mark.asyncio
    async def test_collects_tools(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather, query_genie])
        config = AgentConfig(name="test")

        ctx = await setup_agent(app, agent, config)
        assert len(ctx.tools) == 2

    @pytest.mark.asyncio
    async def test_sub_agent_env_var_expansion(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="test", sub_agents=["$MY_AGENT_URL"])

        with patch.dict("os.environ", {"MY_AGENT_URL": "http://remote.com"}):
            ctx = await setup_agent(app, agent, config)
        assert "http://remote.com" in agent._sub_agent_urls

    @pytest.mark.asyncio
    async def test_sub_agent_missing_env_var_skipped(self):
        app = FastAPI()
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="test", sub_agents=["$MISSING_VAR"])

        with patch.dict("os.environ", {}, clear=True):
            ctx = await setup_agent(app, agent, config)
        # Should not crash, just skip


# ---------------------------------------------------------------------------
# Protocol routes integration
# ---------------------------------------------------------------------------


class TestProtocolRoutes:
    @pytest.fixture
    def app_with_agent(self):
        """Build a FastAPI app with agent protocol mounted."""
        app = FastAPI()
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="test-agent", description="A test agent")
        tools = agent.collect_tools()

        from apx_agent._models import A2ASkill, AgentCard
        card = AgentCard(
            name=config.name,
            description=config.description,
            skills=[
                A2ASkill(id=t.name, name=t.name, description=t.description)
                for t in tools
            ],
        )
        ctx = AgentContext(config=config, tools=tools, card=card, agent=agent)
        app.state.agent_context = ctx
        app.state.mcp_server = None  # no MCP

        _mount_protocol_routes(app)

        # Mount tool routers
        for router in agent.get_tool_routers():
            app.include_router(router, prefix=config.api_prefix)

        return app

    @pytest.mark.asyncio
    async def test_health_endpoint(self, app_with_agent):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_agent),
            base_url="http://test",
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_agent_card_endpoint(self, app_with_agent):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_agent),
            base_url="http://test",
        ) as client:
            resp = await client.get("/.well-known/agent.json")
            assert resp.status_code == 200
            data = resp.json()
            assert data["name"] == "test-agent"
            assert data["url"] == "http://test"
            assert data["mcpEndpoint"] is None  # no MCP server
            assert len(data["skills"]) == 1

    @pytest.mark.asyncio
    async def test_agent_card_404_when_no_context(self):
        app = FastAPI()
        app.state.agent_context = None
        _mount_protocol_routes(app)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/.well-known/agent.json")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_tool_route_invocation(self, app_with_agent):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_agent),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                "/api/tools/get_weather",
                json={"city": "Portland"},
            )
            assert resp.status_code == 200
            assert "Portland" in resp.text

    @pytest.mark.asyncio
    async def test_mcp_sse_503_when_disabled(self, app_with_agent):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_agent),
            base_url="http://test",
        ) as client:
            resp = await client.get("/mcp/sse")
            assert resp.status_code == 503


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------


class TestCreateApp:
    def test_returns_fastapi_instance(self):
        agent = Agent(tools=[get_weather])
        config = AgentConfig(name="test")
        app = create_app(agent, config)
        assert isinstance(app, FastAPI)
