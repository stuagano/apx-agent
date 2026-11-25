"""unity_gateway_mcp (#1268): system.ai.* Unity Gateway MCP services as agent tools.

The MCP session is mocked. Live calls to the Beta services (dbsql, web_search,
sandbox) need the Enhanced Unity Gateway account preview and are manual checks.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from databricks.sdk.errors import PermissionDenied
from databricks.sdk.errors.base import DatabricksError

import apx_agent._unity_gateway_mcp as ug
from apx_agent import (
    Agent,
    ServiceBinding,
    collect_service_bindings,
    mcp_tool,
    sandbox_tool,
    unity_gateway_mcp,
    unity_gateway_mcp_url,
)
from apx_agent._apps_authorization import compile_authorization_plan, infer_operation_authorization
from apx_agent._doctor import Status, check_service_binding, check_workspace_managed_mcp

HOST = "https://ws.cloud.databricks.com"
SERVICES = ["system.ai.dbsql", "system.ai.web_search", "system.ai.sandbox", "system.ai.github"]


def _session(*, tools=()):
    s = MagicMock()
    s.list_tools = AsyncMock(return_value=SimpleNamespace(
        tools=[SimpleNamespace(name=n, description=f"{n} desc") for n in tools]))
    s.call_tool = AsyncMock(return_value=SimpleNamespace(
        structuredContent={"answer": 42}, content=[], isError=False))
    return s


def _patch_open(session):
    seen: list[tuple[str, dict]] = []

    @asynccontextmanager
    async def _open(url, headers, transport, *, timeout_s=30.0):
        seen.append((url, headers))
        yield session

    return patch("apx_agent.mcp_consume._open_session", _open), seen


# ---------------------------------------------------------------------------
# URL / scope generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_url_is_the_unity_gateway_mcp_services_path(service):
    assert unity_gateway_mcp_url(service, workspace_host="ws.cloud.databricks.com/") == (
        f"{HOST}/ai-gateway/mcp-services/{service}")


@pytest.mark.parametrize("service", ["web_search", "system.ai", "system..dbsql"])
def test_url_rejects_names_that_are_not_three_part(service):
    with pytest.raises(ValueError, match="catalog.schema.service"):
        unity_gateway_mcp_url(service, workspace_host=HOST)


# ---------------------------------------------------------------------------
# Factory construction and the compiled binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service", SERVICES)
def test_declared_service_compiles_to_a_user_binding_with_the_ai_gateway_scope(service):
    tools = unity_gateway_mcp(service, tools=["run"])

    assert [t.__name__ for t in tools] == [f"{service.rsplit('.', 1)[1]}_run"]
    assert collect_service_bindings(Agent(tools=tools)) == [ServiceBinding(service, "user")]
    assert infer_operation_authorization(tools[0]).execution_identity == "user"
    plan = compile_authorization_plan(Agent(tools=tools), model="")
    assert "ai-gateway" in plan.user_api_scopes
    assert plan.user_resources == () and plan.service_resources == ()


def test_sandbox_mcp_shares_sandbox_tools_binding_without_a_name_clash():
    """#1266 and #1268 bind system.ai.sandbox with one shape; tool names stay distinct."""
    mcp = unity_gateway_mcp("system.ai.sandbox", tools=["run_in_sandbox"])
    tools = [sandbox_tool(), *mcp]

    assert collect_service_bindings(Agent(tools=tools)) == [ServiceBinding("system.ai.sandbox", "user")]
    assert [t.__name__ for t in tools] == ["run_in_sandbox", "sandbox_run_in_sandbox"]


@pytest.mark.parametrize(("service", "tools"), [("main.tools.mine", ["x"]), ("system.ai.dbsql", [])])
def test_factory_rejects_non_system_services_and_empty_tool_lists(service, tools):
    with pytest.raises(ValueError):
        unity_gateway_mcp(service, tools=tools)


def test_discovery_lists_tools_with_the_default_client(monkeypatch):
    ws = MagicMock()
    ws.config.host = HOST
    ws.config.authenticate.return_value = {"Authorization": "Bearer sp"}
    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: ws)
    p, seen = _patch_open(_session(tools=["search", "fetch-page"]))
    with p:
        tools = unity_gateway_mcp("system.ai.web_search")

    assert [t.__name__ for t in tools] == ["web_search_search", "web_search_fetch_page"]
    assert "search desc" in tools[0].__doc__
    assert seen == [(f"{HOST}/ai-gateway/mcp-services/system.ai.web_search", {"Authorization": "Bearer sp"})]


def test_discovery_failure_names_the_enhanced_unity_gateway_preview(monkeypatch):
    ws = MagicMock()
    ws.config.host = HOST

    @asynccontextmanager
    async def _boom(*a, **k):
        raise RuntimeError("404")
        yield

    monkeypatch.setattr("apx_agent._defaults._make_workspace_client", lambda: ws)
    with patch("apx_agent.mcp_consume._open_session", _boom), pytest.raises(ValueError) as err:
        unity_gateway_mcp("system.ai.dbsql")
    assert "Enhanced Unity Gateway" in str(err.value) and "tools=[...]" in str(err.value)


@pytest.mark.asyncio
async def test_call_runs_as_the_caller_on_the_callers_workspace():
    (tool,) = unity_gateway_mcp("system.ai.dbsql", tools=["execute_sql"])
    obo = MagicMock()
    obo.config.host = HOST + "/"
    obo.config.token = "user-token"
    session = _session()
    p, seen = _patch_open(session)
    with p:
        out = await tool(arguments={"query": "select 1"}, ws=obo)

    assert out == {"result": {"answer": 42}, "is_error": False}
    assert seen == [(f"{HOST}/ai-gateway/mcp-services/system.ai.dbsql", {"Authorization": "Bearer user-token"})]
    assert session.call_tool.await_args.args[:2] == ("execute_sql", {"query": "select 1"})


# ---------------------------------------------------------------------------
# apx-agent doctor
# ---------------------------------------------------------------------------


def test_doctor_fails_with_enhanced_unity_gateway_steps_for_a_beta_service():
    ws = MagicMock()
    ws.api_client.do.side_effect = DatabricksError("not enabled", error_code="FEATURE_DISABLED")

    check = check_service_binding(ServiceBinding("system.ai.dbsql"), ws)

    assert check.status is Status.FAIL
    assert check.fix == ug.ENABLE_ENHANCED_UG


def test_doctor_warns_when_the_service_is_not_readable():
    ws = MagicMock()
    ws.api_client.do.side_effect = PermissionDenied("no EXECUTE")

    check = check_service_binding(ServiceBinding("system.ai.github"), ws)

    assert check.status is Status.WARN
    assert check.fix is not None and "EXECUTE on system.ai.github" in check.fix


def test_doctor_names_per_user_sign_in_for_a_connector():
    ws = MagicMock()
    ws.api_client.do.return_value = {"name": "system.ai.slack"}

    check = check_service_binding(ServiceBinding("system.ai.slack"), ws)

    assert check.status is Status.OK
    assert check.fix == ug.CONNECTOR_SIGN_IN


def test_doctor_explains_managed_mcp_servers_for_workspace_endpoints():
    url = f"{HOST}/api/2.0/mcp/functions/main/tools/classify"
    plan = compile_authorization_plan(Agent(tools=[mcp_tool(url, "classify", description="d")]), model="")

    check = check_workspace_managed_mcp(plan)

    assert check is not None and url in check.detail
    assert check.fix is not None and "Managed MCP Servers" in check.fix
    assert check_workspace_managed_mcp(compile_authorization_plan(
        Agent(tools=unity_gateway_mcp("system.ai.web_search", tools=["search"])), model="")) is None
