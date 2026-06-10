"""Tests for /_apx/probe/checks health-check endpoint."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard


def _make_ctx(model: str = "claude-fake", sub_agents: list | None = None) -> AgentContext:
    config = AgentConfig(name="probe-test", model=model, sub_agents=sub_agents or [])
    card = AgentCard(name="probe-test", description="", skills=[])
    return AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]


@pytest.fixture
def app_with_ctx() -> FastAPI:
    app = FastAPI()
    app.state.agent_context = _make_ctx()
    app.include_router(build_dev_ui_router())
    return app


def _patch_workspace_ok():
    return patch(
        "apx_agent._ui_probe.WorkspaceClient" if False else "databricks.sdk.WorkspaceClient",
        return_value=MagicMock(config=MagicMock(host="https://workspace.example.com")),
    )


def _patch_workspace_fail():
    return patch(
        "databricks.sdk.WorkspaceClient",
        side_effect=Exception("bad credentials"),
    )


def _patch_model_ok(text: str = "hello"):
    sdk_instance = AsyncMock()
    sdk_instance.chat.completions.create = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content=text))])
    )
    return patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk_instance)


def _patch_model_fail():
    sdk_instance = AsyncMock()
    sdk_instance.chat.completions.create = AsyncMock(side_effect=Exception("model down"))
    return patch("databricks_openai.AsyncDatabricksOpenAI", return_value=sdk_instance)


def _patch_no_env():
    return patch("apx_agent._ui_probe._find_env_path", return_value=None)


class TestProbeChecks:
    @pytest.mark.asyncio
    async def test_all_green_path(self, app_with_ctx: FastAPI):
        mock_exp = MagicMock()
        mock_exp.name = "test-experiment"
        mock_client_instance = MagicMock()
        mock_client_instance.get_experiment.return_value = mock_exp
        mlflow_env = {"MLFLOW_TRACKING_URI": "databricks", "MLFLOW_EXPERIMENT_ID": "test-123"}

        with _patch_workspace_ok(), _patch_model_ok(), _patch_no_env(), \
             patch.dict(os.environ, mlflow_env), \
             patch("mlflow.tracking.MlflowClient", return_value=mock_client_instance):
            async with AsyncClient(transport=ASGITransport(app=app_with_ctx), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        assert r.status_code == 200
        data = r.json()
        names = {c["name"]: c["status"] for c in data["checks"]}
        assert names["workspace_auth"] == "ok"
        assert names["model"] == "ok"
        assert names["env_vars"] == "skip"
        assert names["sub_agents"] == "skip"
        assert names["mlflow_config"] == "ok"
        assert names["mlflow_export"] == "ok"
        assert names["resources"] == "skip"
        assert names["obo_identity"] == "skip"  # not in Apps context under test
        assert names["conversation_store"] == "skip"  # none configured
        assert data["overall"] == "ok"

    @pytest.mark.asyncio
    async def test_workspace_auth_failure_marks_overall_fail(self, app_with_ctx: FastAPI):
        with _patch_workspace_fail(), _patch_model_ok(), _patch_no_env():
            async with AsyncClient(transport=ASGITransport(app=app_with_ctx), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        data = r.json()
        assert data["overall"] == "fail"
        ws = next(c for c in data["checks"] if c["name"] == "workspace_auth")
        assert ws["status"] == "fail"
        assert "bad credentials" in ws["message"]

    @pytest.mark.asyncio
    async def test_model_failure(self, app_with_ctx: FastAPI):
        with _patch_workspace_ok(), _patch_model_fail(), _patch_no_env():
            async with AsyncClient(transport=ASGITransport(app=app_with_ctx), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        data = r.json()
        model = next(c for c in data["checks"] if c["name"] == "model")
        assert model["status"] == "fail"
        assert "model down" in model["message"]
        assert data["overall"] == "fail"

    @pytest.mark.asyncio
    async def test_model_skip_when_no_model_configured(self):
        app = FastAPI()
        app.state.agent_context = _make_ctx(model="")
        app.include_router(build_dev_ui_router())
        with _patch_workspace_ok(), _patch_no_env():
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        data = r.json()
        model = next(c for c in data["checks"] if c["name"] == "model")
        assert model["status"] == "skip"

    @pytest.mark.asyncio
    async def test_sub_agent_unreachable_marks_fail(self):
        app = FastAPI()
        app.state.agent_context = _make_ctx(sub_agents=["https://does-not-resolve.invalid"])
        app.include_router(build_dev_ui_router())

        # Replace _check_sub_agent with a stub that returns the failure shape.
        async def _stub_check(name: str, url: str):
            return {
                "name": f"sub_agent: {name}",
                "status": "fail",
                "message": f"{url}: name resolution failed",
                "hint": "",
            }

        with _patch_workspace_ok(), _patch_model_ok(), _patch_no_env(), \
             patch("apx_agent._ui_probe._check_sub_agent", new=_stub_check):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        data = r.json()
        sub = next(c for c in data["checks"] if c["name"].startswith("sub_agent:"))
        assert sub["status"] == "fail"
        assert data["overall"] == "fail"

    @pytest.mark.asyncio
    async def test_sub_agent_returns_non_200_marks_warn(self):
        app = FastAPI()
        app.state.agent_context = _make_ctx(sub_agents=["https://other-agent.example.com"])
        app.include_router(build_dev_ui_router())

        async def _stub_check(name: str, url: str):
            return {
                "name": f"sub_agent: {name}",
                "status": "warn",
                "message": f"{url} returned 404",
                "hint": "",
            }

        with _patch_workspace_ok(), _patch_model_ok(), _patch_no_env(), \
             patch("apx_agent._ui_probe._check_sub_agent", new=_stub_check):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        data = r.json()
        sub = next(c for c in data["checks"] if c["name"].startswith("sub_agent:"))
        assert sub["status"] == "warn"
        assert "404" in sub["message"]

    @pytest.mark.asyncio
    async def test_overall_warn_when_only_warns(self, tmp_path, monkeypatch):
        # workspace ok, model ok, env_vars warn (missing var), no sub-agents
        env_path = tmp_path / "test.env"
        env_path.write_text("MISSING_VAR=somevalue\n")
        monkeypatch.delenv("MISSING_VAR", raising=False)
        # H9 added _check_mlflow_read to the probe. If MLFLOW_EXPERIMENT_ID
        # leaks from the ambient environment that check attempts a real span
        # read and can return "fail", flipping overall to "fail". Pin the
        # MLflow env so mlflow_read skips and mlflow_config warns — leaving
        # env_vars + mlflow_config as the only non-skip results (both warn).
        monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)
        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

        with _patch_workspace_ok(), _patch_model_ok(), \
             patch("apx_agent._ui_probe._find_env_path", return_value=env_path), \
             patch("apx_agent._ui_probe._read_env_file", return_value={"MISSING_VAR": "somevalue"}):
            app = FastAPI()
            app.state.agent_context = _make_ctx()
            app.include_router(build_dev_ui_router())
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                r = await ac.get("/_apx/probe/checks")
        data = r.json()
        env = next(c for c in data["checks"] if c["name"] == "env_vars")
        assert env["status"] == "warn"
        assert "MISSING_VAR" in env["message"]
        assert data["overall"] == "warn"


class TestCheckSubAgentDirect:
    """Direct tests for _check_sub_agent that exercise the httpx code path."""

    @pytest.mark.asyncio
    async def test_returns_ok_on_200(self):
        from apx_agent._ui_probe import _check_sub_agent

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))

        class _CM:
            async def __aenter__(self):
                return mock_client
            async def __aexit__(self, *args):
                return None

        with patch("httpx.AsyncClient", return_value=_CM()):
            result = await _check_sub_agent("planner", "https://planner.example.com")

        assert result["status"] == "ok"
        assert "planner" in result["name"]
        # The function appends the agent-card path
        called_url = mock_client.get.call_args.args[0]
        assert called_url.endswith("/.well-known/agent.json")

    @pytest.mark.asyncio
    async def test_returns_warn_on_non_200(self):
        from apx_agent._ui_probe import _check_sub_agent

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=503))

        class _CM:
            async def __aenter__(self):
                return mock_client
            async def __aexit__(self, *args):
                return None

        with patch("httpx.AsyncClient", return_value=_CM()):
            result = await _check_sub_agent("foo", "https://foo.example.com")
        assert result["status"] == "warn"
        assert "503" in result["message"]

    @pytest.mark.asyncio
    async def test_returns_fail_on_exception(self):
        from apx_agent._ui_probe import _check_sub_agent

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("DNS lookup failed"))

        class _CM:
            async def __aenter__(self):
                return mock_client
            async def __aexit__(self, *args):
                return None

        with patch("httpx.AsyncClient", return_value=_CM()):
            result = await _check_sub_agent("foo", "https://nope.invalid")
        assert result["status"] == "fail"
        assert "DNS lookup failed" in result["message"]


class TestCheckAgentSource:
    @pytest.mark.asyncio
    async def test_ok_when_tools_resolve(self, tmp_path):
        from apx_agent._ui_probe import _check_agent_source
        f = tmp_path / "agent.py"
        f.write_text(
            "from apx_agent import Agent, tool\n"
            "@tool\ndef echo(m: str) -> str:\n    'e'\n    return m\n"
            "agent = Agent(tools=[echo], instructions='x')\n"
        )
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=f):
            r = await _check_agent_source()
        assert r["status"] == "ok"

    @pytest.mark.asyncio
    async def test_fail_on_dangling_tool_reference(self, tmp_path):
        from apx_agent._ui_probe import _check_agent_source
        f = tmp_path / "agent.py"
        f.write_text(
            "from apx_agent import Agent\n"
            "agent = Agent(tools=[ghost_tool], instructions='x')\n"
        )
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=f):
            r = await _check_agent_source()
        assert r["status"] == "fail"
        assert "ghost_tool" in r["message"]

    @pytest.mark.asyncio
    async def test_fail_on_syntax_error(self, tmp_path):
        from apx_agent._ui_probe import _check_agent_source
        f = tmp_path / "agent.py"
        f.write_text("agent = Agent(tools=[\n")
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=f):
            r = await _check_agent_source()
        assert r["status"] == "fail" and "syntax" in r["message"].lower()

    @pytest.mark.asyncio
    async def test_skip_when_no_file(self):
        from apx_agent._ui_probe import _check_agent_source
        with patch("apx_agent._ui_edit._find_agent_router_path", return_value=None):
            r = await _check_agent_source()
        assert r["status"] == "skip"


class TestProbeUiRoute:
    @pytest.mark.asyncio
    async def test_probe_returns_html(self, app_with_ctx: FastAPI):
        async with AsyncClient(transport=ASGITransport(app=app_with_ctx), base_url="http://test") as ac:
            r = await ac.get("/_apx/probe")
        assert r.status_code == 200
        assert "Health checks" in r.text
        assert "/_apx/probe/checks" in r.text  # JS fetches the JSON endpoint


class TestCheckObo:
    @pytest.mark.asyncio
    async def test_skips_outside_apps(self):
        from apx_agent._ui_probe import _check_obo
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DATABRICKS_APP_PORT", None)
            result = await _check_obo(headers={})
        assert result["status"] == "skip"

    @pytest.mark.asyncio
    async def test_ok_when_token_present(self):
        from apx_agent._ui_probe import _check_obo
        headers = {
            "x-forwarded-access-token": "tok-abc",
            "x-forwarded-email": "user@example.com",
        }
        result = await _check_obo(headers=headers)
        assert result["status"] == "ok"
        assert "user@example.com" in result["message"]

    @pytest.mark.asyncio
    async def test_fail_in_apps_without_token(self):
        from apx_agent._ui_probe import _check_obo
        # X-Forwarded-Host present (proxy) but no access token → OBO not enabled.
        headers = {"x-forwarded-host": "app.example.com"}
        result = await _check_obo(headers=headers)
        assert result["status"] == "fail"
        assert "OBO" in result["hint"]


class TestCheckConversationStore:
    @pytest.mark.asyncio
    async def test_skips_when_none(self):
        from apx_agent._ui_probe import _check_conversation_store
        result = await _check_conversation_store(None)
        assert result["status"] == "skip"

    @pytest.mark.asyncio
    async def test_inmemory_ok_without_ping(self):
        from apx_agent import InMemoryConversationStore
        from apx_agent._ui_probe import _check_conversation_store
        result = await _check_conversation_store(InMemoryConversationStore())
        assert result["status"] == "ok"
        assert "in-process" in result["message"]

    @pytest.mark.asyncio
    async def test_ping_reachable(self):
        from apx_agent._ui_probe import _check_conversation_store
        store = MagicMock()
        store.get_conversation = MagicMock(return_value=None)
        result = await _check_conversation_store(store)
        assert result["status"] == "ok"
        store.get_conversation.assert_called_once()

    @pytest.mark.asyncio
    async def test_ping_failure(self):
        from apx_agent._ui_probe import _check_conversation_store
        store = MagicMock()
        store.get_conversation = MagicMock(side_effect=Exception("connection refused"))
        result = await _check_conversation_store(store)
        assert result["status"] == "fail"
        assert "connection refused" in result["message"]


class TestCheckResources:
    @pytest.mark.asyncio
    async def test_skips_when_no_resources(self):
        from apx_agent._ui_probe import _check_resources
        ctx = _make_ctx()
        result = await _check_resources(ctx)
        assert result["status"] == "skip"

    @pytest.mark.asyncio
    async def test_verifies_uc_function_via_positional(self):
        from apx_agent import LlmAgent, uc_function_tool
        from apx_agent._ui_probe import _check_resources

        agent = LlmAgent(tools=[uc_function_tool("main.tools.lookup")])
        config = AgentConfig(name="r", model="m")
        ctx = AgentContext(
            config=config, tools=[],
            card=AgentCard(name="r", description="", skills=[]), agent=agent,
        )
        ws = MagicMock()
        ws.functions.get = MagicMock(return_value=MagicMock())
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            result = await _check_resources(ctx)
        assert result["status"] == "ok"
        # Verify positional call (version-agnostic), not keyword.
        ws.functions.get.assert_called_once_with("main.tools.lookup")

    @pytest.mark.asyncio
    async def test_reports_unreachable_resource(self):
        from apx_agent import LlmAgent, uc_function_tool
        from apx_agent._ui_probe import _check_resources

        agent = LlmAgent(tools=[uc_function_tool("main.tools.missing")])
        config = AgentConfig(name="r", model="m")
        ctx = AgentContext(
            config=config, tools=[],
            card=AgentCard(name="r", description="", skills=[]), agent=agent,
        )
        ws = MagicMock()
        ws.functions.get = MagicMock(side_effect=Exception("does not exist"))
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            result = await _check_resources(ctx)
        assert result["status"] == "fail"
        assert "missing" in result["message"]


class TestCheckMlflowExportCategories:
    @pytest.mark.asyncio
    async def test_egress_blocked_category(self):
        from apx_agent import _mlflow_tracing
        from apx_agent._ui_probe import _check_mlflow_export
        _mlflow_tracing._mlflow_export_errors.clear()
        _mlflow_tracing._mlflow_export_errors.append(
            "Failed to send trace: Connection refused to "
            "us-east-1.storage.cloud.databricks.com:443"
        )
        result = await _check_mlflow_export()
        assert result["status"] == "warn"
        assert "egress_blocked" in result["message"]
        assert "private-link" in result["hint"]
        _mlflow_tracing._mlflow_export_errors.clear()

    @pytest.mark.asyncio
    async def test_write_denied_category(self):
        from apx_agent import _mlflow_tracing
        from apx_agent._ui_probe import _check_mlflow_export
        _mlflow_tracing._mlflow_export_errors.clear()
        _mlflow_tracing._mlflow_export_errors.append(
            "Failed to log trace: PERMISSION_DENIED on experiment"
        )
        result = await _check_mlflow_export()
        assert result["status"] == "warn"
        assert "write_denied" in result["message"]
        assert "EDIT" in result["hint"]
        _mlflow_tracing._mlflow_export_errors.clear()


class TestMlflowConfigPyprojectFallback:
    """_check_mlflow_config must not warn when experiment is set via pyproject.toml."""

    @pytest.mark.asyncio
    async def test_pyproject_experiment_suppresses_missing_id_warn(self, monkeypatch):
        from apx_agent._ui_probe import _check_mlflow_config

        monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
        monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)

        fake_exp = MagicMock()
        fake_exp.name = "/Users/x/my-agent-experiment"

        with patch("apx_agent._ui_probe._pyproject_experiment", return_value="my-agent-experiment"), \
             patch("mlflow.tracking.MlflowClient") as mock_client_cls:
            mock_client_cls.return_value.get_experiment_by_name.return_value = fake_exp
            result = await _check_mlflow_config()

        assert result["status"] == "ok"
        assert "pyproject.toml" in result["message"]

    @pytest.mark.asyncio
    async def test_no_experiment_anywhere_warns(self, monkeypatch):
        from apx_agent._ui_probe import _check_mlflow_config

        monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
        monkeypatch.delenv("MLFLOW_EXPERIMENT_ID", raising=False)

        with patch("apx_agent._ui_probe._pyproject_experiment", return_value=""):
            result = await _check_mlflow_config()

        assert result["status"] == "warn"
        assert "pyproject.toml" in result["hint"]

    @pytest.mark.asyncio
    async def test_env_var_id_takes_precedence(self, monkeypatch):
        from apx_agent._ui_probe import _check_mlflow_config

        monkeypatch.setenv("MLFLOW_TRACKING_URI", "databricks")
        monkeypatch.setenv("MLFLOW_EXPERIMENT_ID", "42")

        fake_exp = MagicMock()
        fake_exp.name = "env-exp"

        with patch("apx_agent._ui_probe._pyproject_experiment", return_value="pyproject-exp"), \
             patch("mlflow.tracking.MlflowClient") as mock_client_cls:
            mock_client_cls.return_value.get_experiment.return_value = fake_exp
            result = await _check_mlflow_config()

        assert result["status"] == "ok"
        assert "env" in result["message"]
        mock_client_cls.return_value.get_experiment.assert_called_once_with("42")


class TestInvocationsSessionBridge:
    """context.conversation_id must be bridged to custom_inputs.session_id."""

    @pytest.mark.asyncio
    async def test_context_conversation_id_injected(self):
        from apx_agent._invocations import mount_invocations_route
        from apx_agent import LlmAgent, AgentConfig
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import MagicMock, patch
        from mlflow.types.agent import ChatAgentResponse

        agent = LlmAgent(tools=[])
        config = AgentConfig(name="sess-test", model="fake-model")

        captured: dict = {}
        mock_ca = MagicMock()
        mock_ca.predict.return_value = ChatAgentResponse(messages=[])

        def _spy(agent_arg, *, model, conversation_store=None):
            captured["chat_agent"] = mock_ca
            return mock_ca

        app = FastAPI()
        with patch("apx_agent._chat_agent.chat_agent_for", side_effect=_spy):
            mount_invocations_route(app, agent, config)
            client = TestClient(app)
            client.post(
                "/invocations",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "context": {"conversation_id": "conv-abc-123"},
                },
            )

        call_kwargs = mock_ca.predict.call_args.kwargs
        custom_inputs = call_kwargs.get("custom_inputs") or {}
        assert custom_inputs.get("session_id") == "conv-abc-123"

    @pytest.mark.asyncio
    async def test_explicit_session_id_not_overridden(self):
        from apx_agent._invocations import mount_invocations_route
        from apx_agent import LlmAgent, AgentConfig
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import MagicMock, patch
        from mlflow.types.agent import ChatAgentResponse

        agent = LlmAgent(tools=[])
        config = AgentConfig(name="sess-test2", model="fake-model")

        captured: dict = {}
        mock_ca = MagicMock()
        mock_ca.predict.return_value = ChatAgentResponse(messages=[])

        def _spy(agent_arg, *, model, conversation_store=None):
            captured["chat_agent"] = mock_ca
            return mock_ca

        app = FastAPI()
        with patch("apx_agent._chat_agent.chat_agent_for", side_effect=_spy):
            mount_invocations_route(app, agent, config)
            client = TestClient(app)
            client.post(
                "/invocations",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "custom_inputs": {"session_id": "explicit-id"},
                    "context": {"conversation_id": "conv-should-not-win"},
                },
            )

        call_kwargs = mock_ca.predict.call_args.kwargs
        custom_inputs = call_kwargs.get("custom_inputs") or {}
        assert custom_inputs.get("session_id") == "explicit-id"
