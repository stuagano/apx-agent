"""Tests for _wiring.py — setup_agent(), create_app(), and protocol routes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from apx_agent import Agent, LlmAgent, AgentConfig, AgentContext, create_app, setup_agent
from apx_agent._wiring import (
    _install_responses_input_adapter,
    _mount_protocol_routes,
    apply_config_knobs,
)

from .conftest import get_weather, query_genie


# ---------------------------------------------------------------------------
# setup_agent
# ---------------------------------------------------------------------------


class TestSetupAgent:
    @pytest.mark.asyncio
    async def test_wires_protocol_routes(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="test-agent", description="Test")

        ctx = await setup_agent(app, agent, config)
        assert ctx is not None
        assert ctx.config.name == "test-agent"
        assert hasattr(app.state, "agent_context")
        assert app.state.agent_context is ctx

        # Check protocol routes exist
        route_paths = [r.path for r in app.routes]
        assert "/.well-known/agent.json" in route_paths
        assert "/health" in route_paths
        # /invocations is mounted by create_app's lifespan, not setup_agent
        # directly, so it doesn't appear in this list — see test_invocations_route.py.

    @pytest.mark.asyncio
    async def test_mounts_tool_routes(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="test-agent", api_prefix="/api")

        await setup_agent(app, agent, config)
        route_paths = [r.path for r in app.routes]
        assert "/api/tools/get_weather" in route_paths

    @pytest.mark.asyncio
    async def test_returns_none_when_no_config(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])

        with patch("apx_agent._wiring._load_agent_config", return_value=None):
            ctx = await setup_agent(app, agent, config=None)
        assert ctx is None
        assert app.state.agent_context is None

    @pytest.mark.asyncio
    async def test_collects_tools(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather, query_genie])
        config = AgentConfig(name="test")

        ctx = await setup_agent(app, agent, config)
        assert len(ctx.tools) == 2

    @pytest.mark.asyncio
    async def test_sub_agent_env_var_expansion(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="test", sub_agents=["$MY_AGENT_URL"])

        with patch.dict("os.environ", {"MY_AGENT_URL": "http://remote.com"}):
            ctx = await setup_agent(app, agent, config)
        assert "http://remote.com" in agent._sub_agent_urls

    @pytest.mark.asyncio
    async def test_sub_agent_missing_env_var_skipped(self):
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="test", sub_agents=["$MISSING_VAR"])

        with patch.dict("os.environ", {}, clear=True):
            ctx = await setup_agent(app, agent, config)
        # Should not crash, just skip

    @pytest.mark.asyncio
    async def test_config_generation_knobs_applied_when_constructor_unset(self):
        # The compile path reads temperature/max_tokens/max_iterations off the
        # instance; config must be merged onto it during setup.
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])  # all knobs default to None
        config = AgentConfig(
            name="test", temperature=0.7, max_tokens=512, max_iterations=10
        )

        await setup_agent(app, agent, config)
        assert agent._temperature == 0.7
        assert agent._max_tokens == 512
        # max_iterations default (10) applies when constructor left it None
        assert agent._max_iterations == 10

    @pytest.mark.asyncio
    async def test_constructor_generation_knobs_win_over_config(self):
        app = FastAPI()
        agent = LlmAgent(
            tools=[get_weather],
            temperature=0.0,  # deliberate 0.0 must not be clobbered
            max_tokens=256,
            max_iterations=5,
        )
        config = AgentConfig(
            name="test", temperature=0.9, max_tokens=1024, max_iterations=10
        )

        await setup_agent(app, agent, config)
        assert agent._temperature == 0.0
        assert agent._max_tokens == 256
        assert agent._max_iterations == 5


# ---------------------------------------------------------------------------
# apply_config_knobs — shared seam used by both setup_agent AND apx deploy
# (model-serving). The deploy path calls this directly before log_agent, so
# these tests cover the deploy-path application without standing up the CLI.
# ---------------------------------------------------------------------------


class TestApplyConfigKnobs:
    def test_deploy_path_applies_knobs_when_constructor_unset(self):
        # Mirror the deploy call site: build an AgentConfig from a
        # [tool.apx.agent] dict (filtered to known fields), then apply.
        agent = LlmAgent(tools=[get_weather])  # all knobs default to None
        cfg_dict = {
            "name": "deployed-agent",
            "temperature": 0.3,
            "max_tokens": 800,
            "max_iterations": 10,
            "unknown_field": "ignored",
        }
        config = AgentConfig(
            **{k: v for k, v in cfg_dict.items() if k in AgentConfig.model_fields}
        )

        apply_config_knobs(agent, config)
        assert agent._temperature == 0.3
        assert agent._max_tokens == 800
        assert agent._max_iterations == 10

    def test_deploy_path_constructor_wins(self):
        agent = LlmAgent(
            tools=[get_weather],
            temperature=0.0,  # deliberate 0.0 must survive
            max_tokens=128,
            max_iterations=3,
        )
        config = AgentConfig(
            name="deployed-agent",
            temperature=0.9,
            max_tokens=4096,
            max_iterations=10,
        )

        apply_config_knobs(agent, config)
        assert agent._temperature == 0.0
        assert agent._max_tokens == 128
        assert agent._max_iterations == 3

    def test_composition_agent_without_knob_attrs_is_safe(self):
        # SequentialAgent-style roots don't define _temperature/_max_tokens;
        # the hasattr guard must skip them rather than crash.
        from apx_agent import SequentialAgent

        inner = LlmAgent(tools=[get_weather])
        root = SequentialAgent(agents=[inner])
        config = AgentConfig(name="seq", temperature=0.5, max_tokens=200)

        apply_config_knobs(root, config)  # must not raise
        assert not hasattr(root, "_temperature")

    def test_idempotent(self):
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="x", temperature=0.4, max_iterations=10)

        apply_config_knobs(agent, config)
        apply_config_knobs(agent, config)  # second call no-ops
        assert agent._temperature == 0.4
        assert agent._max_iterations == 10


# ---------------------------------------------------------------------------
# Protocol routes integration
# ---------------------------------------------------------------------------


class TestProtocolRoutes:
    @pytest.fixture
    def app_with_agent(self):
        """Build a FastAPI app with agent protocol mounted."""
        app = FastAPI()
        agent = LlmAgent(tools=[get_weather])
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
        agent = LlmAgent(tools=[get_weather])
        config = AgentConfig(name="test")
        app = create_app(agent, config)
        assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# /responses string-input adapter
# ---------------------------------------------------------------------------


class TestResponsesStringInputAdapter:
    """The OpenAI Responses API accepts ``input: string`` or ``input: list``;
    the upstream ResponsesAgentRequest schema only accepts the list form.
    The adapter rewrites a string ``input`` to the canonical user-message
    envelope so curl-first users don't hit a confusing 400."""

    def _app_with_adapter(self) -> FastAPI:
        """Tiny FastAPI app with the adapter installed + an echo /responses
        that returns whatever payload the upstream handler sees post-adapter."""
        app = FastAPI()
        _install_responses_input_adapter(app)

        @app.post("/responses")
        async def echo(request: Request) -> JSONResponse:
            return JSONResponse(await request.json())

        return app

    @pytest.mark.asyncio
    async def test_string_input_is_rewritten_to_user_message_list(self):
        app = self._app_with_adapter()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as ac:
            r = await ac.post("/responses", json={"input": "hello", "stream": False})
        assert r.status_code == 200
        body = r.json()
        assert body["input"] == [{"role": "user", "content": "hello"}]
        assert body["stream"] is False

    @pytest.mark.asyncio
    async def test_list_input_passes_through_unchanged(self):
        """List-form input must not be touched — it's the canonical shape."""
        app = self._app_with_adapter()
        payload = {
            "input": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as ac:
            r = await ac.post("/responses", json=payload)
        assert r.status_code == 200
        assert r.json() == payload

    @pytest.mark.asyncio
    async def test_other_routes_are_not_touched(self):
        """The adapter only rewrites POST /responses — leave everything else
        alone (the same body shape may mean different things on /invocations)."""
        app = FastAPI()
        _install_responses_input_adapter(app)

        @app.post("/invocations")
        async def echo(request: Request) -> JSONResponse:
            return JSONResponse(await request.json())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as ac:
            r = await ac.post("/invocations", json={"input": "hello"})
        assert r.status_code == 200
        # input untouched on /invocations
        assert r.json() == {"input": "hello"}

    @pytest.mark.asyncio
    async def test_malformed_json_is_forwarded_unchanged(self):
        """The adapter must not crash on a malformed body — it forwards the
        bytes through and lets the real handler return its native 4xx."""
        app = FastAPI()
        _install_responses_input_adapter(app)

        @app.post("/responses")
        async def echo_bytes(request: Request) -> JSONResponse:
            body = await request.body()
            return JSONResponse({"received": body.decode("latin-1")})

        bad = b"not json at all"
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as ac:
            r = await ac.post(
                "/responses",
                content=bad,
                headers={"content-type": "application/json"},
            )
        assert r.status_code == 200
        assert r.json()["received"] == bad.decode("latin-1")


# ---------------------------------------------------------------------------
# apply_config_knobs — persona instruction overlay
# ---------------------------------------------------------------------------


def _cfg(**kw):
    return AgentConfig(name="t", **kw)


def test_instructions_compose_when_both_present():
    agent = Agent(tools=[], instructions="GROUNDING")
    apply_config_knobs(agent, _cfg(instructions="PERSONA"))
    assert agent._instructions.index("PERSONA") < agent._instructions.index("GROUNDING")


def test_instructions_fill_when_agent_empty():
    agent = Agent(tools=[], instructions="")
    apply_config_knobs(agent, _cfg(instructions="PERSONA"))
    assert agent._instructions == "PERSONA"


def test_plain_agent_no_envelope_instructions_unchanged():
    agent = Agent(tools=[], instructions="GROUNDING")
    apply_config_knobs(agent, _cfg(instructions=""))
    assert agent._instructions == "GROUNDING"


def test_instruction_overlay_is_idempotent():
    agent = Agent(tools=[], instructions="GROUNDING")
    cfg = _cfg(instructions="PERSONA")
    apply_config_knobs(agent, cfg)
    once = agent._instructions
    apply_config_knobs(agent, cfg)
    assert agent._instructions == once
