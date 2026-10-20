from __future__ import annotations

from apx_agent import Agent, SequentialAgent, platform_tools
from apx_agent._platform_tools import PlatformToolRef, platform_tool_leaves


def _noop(q: str) -> str:
    """Echo."""
    return q


def test_platform_tools_splats_into_tools_and_stays_out_of_callables() -> None:
    agent = Agent(name="a", tools=[_noop, *platform_tools("genie-pricing")])
    assert agent._tool_fns == [_noop]
    assert agent._platform_tool_refs == [PlatformToolRef(frozenset({"genie-pricing"}))]
    assert len(agent._analyzed) == 1


def test_no_ids_means_all() -> None:
    [ref] = platform_tools()
    assert ref.ids == frozenset()


def test_leaves_found_in_composites_including_shared_ids() -> None:
    a = Agent(name="a", tools=[*platform_tools("g")])
    b = Agent(name="b", tools=[*platform_tools("g")])
    leaves = platform_tool_leaves(SequentialAgent([a, b], name="s"))
    assert [(leaf._name, ref.ids) for leaf, ref in leaves] == [
        ("a", frozenset({"g"})), ("b", frozenset({"g"})),
    ]


# --- materialization through AgentKit -------------------------------------------------
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

_BASE = 'schema_version = 1\n\n[agent]\nframework = "langgraph"\nserver = "agentbricks"\n'
_SPACE = "01ef0123456789abcdef0123456789ab"


def _genie(tool_id: str) -> str:
    return (
        f'\n[[tools]]\nid = "{tool_id}"\nauth = "user"\n'
        f'[tools.source]\nkind = "genie_agent"\nspace_id = "{_SPACE}"\n'
    )


def test_genie_tools_resolve_identity_per_call(tmp_path: Path) -> None:
    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(_BASE + _genie("pricing"))
    calls: list[str] = []

    def client_for(mode: str) -> SimpleNamespace:
        calls.append(mode)
        return SimpleNamespace()

    tools = static_platform_tools(tmp_path, client_for)
    assert [t.name for t in tools["pricing"]] == ["pricing_ask", "pricing_poll", "pricing_query_result"]
    assert calls == []  # construction does no auth; GenieAgent calls client_for at call time


def test_genie_id_containing_query_groups_under_its_id(tmp_path: Path) -> None:
    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(_BASE + _genie("my_query"))
    tools = static_platform_tools(tmp_path, lambda mode: SimpleNamespace())
    assert list(tools) == ["my_query"]
    assert len(tools["my_query"]) == 3


def test_project_root_is_pinned(tmp_path: Path, monkeypatch) -> None:
    import os

    from apx_agent._platform_tools import static_platform_tools

    (tmp_path / "agent.toml").write_text(_BASE + _genie("pricing"))
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    static_platform_tools(tmp_path, lambda mode: SimpleNamespace())
    assert "AGENTBRICKS_PROJECT_ROOT" not in os.environ  # pinned only for the load, then restored


def test_private_mcp_seam_still_exists() -> None:
    """Fails loudly when a databricks-agentbricks bump removes the one private symbol we use."""
    from databricks_agentkit.langgraph import mcp

    assert callable(mcp._server_from_tool)


# --- compiled into the graph, under apx guards ----------------------------------------
import asyncio  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402


def _mcp(tool_id: str, auth: str) -> str:
    return (
        f'\n[[tools]]\nid = "{tool_id}"\nauth = "{auth}"\n'
        f'[tools.source]\nkind = "mcp"\nservice = "main.mcp.{tool_id}"\n'
    )


def _fake_tool(name: str, effect: Any = None) -> Any:
    from langchain_core.tools import tool

    @tool(name)
    def _t(q: str) -> str:
        """Platform tool."""
        return f"platform:{q}" if effect is None else effect()

    return _t


def _tool_calling_model(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _ToolFake(GenericFakeChatModel):  # same pattern as tests/test_a2a.py
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    model = _ToolFake(messages=iter([
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": {"q": "x"}, "id": "c1"}]),
        AIMessage(content="done"),
    ]))
    monkeypatch.setattr("apx_agent._compile._build_chat_databricks", lambda *a, **k: model)


def test_platform_tool_runs_under_before_tool_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_core.messages import HumanMessage, ToolMessage

    from apx_agent import compile_to_langgraph

    seen: list[str] = []

    def guard(name: str, args: dict[str, Any]) -> None:
        seen.append(name)

    _tool_calling_model(monkeypatch, "pricing_ask")
    agent = Agent(name="a", tools=[*platform_tools("pricing")], before_tool=guard)
    graph = compile_to_langgraph(agent, ws=None, model="m",
                                 platform_tools={"pricing": [_fake_tool("pricing_ask")],
                                                 "other": [_fake_tool("other_ask")]})
    result = graph.invoke({"messages": [HumanMessage(content="go")]})
    assert seen == ["pricing_ask"]
    [tool_msg] = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msg.content == "platform:x"


def test_toolscope_on_platform_tool_is_rejected() -> None:
    from apx_agent._tool_scope import ToolScope, attach_scope

    [ref] = platform_tools("pricing")
    with pytest.raises(ValueError, match="downscope"):
        attach_scope(ref, ToolScope(catalogs=("sales",)))


def test_responses_target_rejects_platform_tools() -> None:
    from apx_agent import compile_agent

    with pytest.raises(ValueError, match="durable_agent_server"):
        compile_agent(Agent(name="a", tools=[*platform_tools()]), model="m",
                      target="responses_agent", service_ws=MagicMock())


def test_user_tool_without_request_resolver_surfaces_sdk_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deployed (DATABRICKS_APP_NAME set) user tool with no resolver: AgentKit raises; apx adds no fallback."""
    from databricks_agentkit.runtime.auth import AuthError
    from databricks_agentkit.runtime.tool_manifest import load_tools

    from apx_agent._platform_tools import _mcp_server_for

    (tmp_path / "agent.toml").write_text(_BASE + _mcp("crm", "user"))
    monkeypatch.setenv("AGENTBRICKS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_APP_NAME", "app")
    [record] = load_tools(expected_framework="langgraph")
    assert record.auth == "user"
    with pytest.raises(AuthError):
        _mcp_server_for(record, None)


def test_mcp_records_split_by_auth_between_startup_and_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-auth MCP is never built once and shared; app-auth MCP never per request."""
    from apx_agent import _platform_tools as pt

    (tmp_path / "agent.toml").write_text(
        _BASE + _mcp("shared", "app") + _mcp("crm", "user") + _mcp("hr", "user")
    )
    built: list[str] = []
    served: list[list[str]] = []

    def fake_server(record: Any, client_for: Any) -> SimpleNamespace:
        built.append(record.id)
        return SimpleNamespace(name=record.id)

    class _Client:
        async def get_tools(self, *, server_name: str) -> list[str]:
            return [server_name]

    def fake_client(servers: list[Any], **kwargs: Any) -> _Client:
        served.append([s.name for s in servers])
        return _Client()

    monkeypatch.setattr(pt, "_mcp_server_for", fake_server)
    monkeypatch.setattr("databricks_agentkit.langgraph.mcp.mcp_client", fake_client)

    assert pt.static_platform_tools(tmp_path, lambda mode: SimpleNamespace()) == {"shared": ["shared"]}
    assert (built, served) == (["shared"], [["shared"]])

    built.clear(), served.clear()
    got = asyncio.run(pt.request_platform_tools(tmp_path, lambda mode: SimpleNamespace(), frozenset({"crm"})))
    assert got == {"crm": ["crm"]}
    assert (built, served) == (["crm"], [["crm"]])

    built.clear(), served.clear()
    got = asyncio.run(pt.request_platform_tools(tmp_path, lambda mode: SimpleNamespace(), frozenset()))
    assert got == {"crm": ["crm"], "hr": ["hr"]}
    assert (built, served) == (["crm", "hr"], [["crm", "hr"]])


def test_platform_client_for_user_requires_request_and_app_is_service() -> None:
    from apx_agent._durable_agent import _REQUEST_AUTH, platform_client_for

    service, user = MagicMock(name="service"), MagicMock(name="user")
    client_for = platform_client_for(service)
    assert client_for("app") is service
    with pytest.raises(RuntimeError, match="needs a request user"):
        client_for("user")
    auth = SimpleNamespace(client_for=lambda mode: user if mode == "user" else None)
    token = _REQUEST_AUTH.set(auth)
    try:
        assert client_for("user") is user
    finally:
        _REQUEST_AUTH.reset(token)
    with pytest.raises(RuntimeError, match="needs a request user"):
        client_for("user")


def test_durable_turn_resolves_platform_user_to_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A startup-built tool, called mid-turn, gets this request's user — not the service client."""
    from apx_agent._durable_agent import compile_durable_handlers, platform_client_for

    service, user = MagicMock(name="service"), MagicMock(name="user")
    client_for = platform_client_for(service)
    resolved: list[Any] = []

    def effect() -> str:
        resolved.append(client_for("user"))
        return "ok"

    _tool_calling_model(monkeypatch, "pricing_ask")
    agent = Agent(name="a", tools=[*platform_tools("pricing")])
    handlers = compile_durable_handlers(agent, model="m", service_ws=service,
                                        platform_tools={"pricing": [_fake_tool("pricing_ask", effect)]})
    from databricks_agentkit.runtime.auth import RequestAuthContext

    user.current_user.me.return_value.id = "user-a"
    monkeypatch.setattr(RequestAuthContext, "client_for", lambda self, mode: user)
    auth = RequestAuthContext(token=None, principal="user-a", local=True)
    context = SimpleNamespace(request_auth=auth, session_id="s", is_recovery=False)
    result = asyncio.run(handlers.invoke({"messages": [{"role": "user", "content": "go"}]}, context))
    assert result["status"] == "completed"
    assert resolved == [user]
    with pytest.raises(RuntimeError):  # the turn's identity does not leak past it
        client_for("user")


def test_durable_server_needs_agent_toml_for_platform_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import compile_agent

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    with pytest.raises(ValueError, match=r"agent\.toml"):
        compile_agent(Agent(name="a", tools=[*platform_tools("pricing")]), target="durable_agent_server",
                      model="m", service_ws=MagicMock(), project_root=tmp_path)


def test_durable_server_undeclared_platform_id_fails_compile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import compile_agent

    (tmp_path / "agent.toml").write_text(_BASE + _genie("pricing"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    with pytest.raises(ValueError, match="not declared"):
        compile_agent(Agent(name="a", tools=[*platform_tools("missing")]), target="durable_agent_server",
                      model="m", service_ws=MagicMock(), project_root=tmp_path)


def test_platform_materialization_failure_degrades_readyz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from apx_agent import compile_agent

    (tmp_path / "agent.toml").write_text(_BASE + _genie("pricing"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBRICKS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DATABRICKS_AGENTBRICKS_RUNTIME_STORE_LOCAL", "true")

    def offline(*a: Any, **k: Any) -> Any:
        raise ConnectionError("mcp offline")

    monkeypatch.setattr("apx_agent._platform_tools.static_platform_tools", offline)
    app = compile_agent(Agent(name="a", tools=[*platform_tools("pricing")]), target="durable_agent_server",
                        model="m", service_ws=MagicMock(), project_root=tmp_path)
    with TestClient(app) as client:
        readiness = client.get("/readyz")
    assert readiness.status_code == 503
    assert readiness.json()["checks"] == {"platform_tools": "ConnectionError: mcp offline"}
