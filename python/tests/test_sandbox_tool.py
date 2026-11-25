"""sandbox_tool (#1266): Databricks Sandbox execution, its system.ai.sandbox binding, doctor probe.

The Sandbox REST API is called through ``ws.api_client.do`` and mocked here.
Live execution needs the workspace Sandbox preview and is a manual check
(docs/tools/overview.md), not part of CI.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.errors.base import DatabricksError

import apx_agent.sandbox as sb
from apx_agent import (
    Agent,
    SequentialAgent,
    ServiceBinding,
    collect_service_bindings,
    sandbox_tool,
)
from apx_agent._apps_authorization import compile_authorization_plan, infer_operation_authorization
from apx_agent._doctor import Status, check_service_binding
from apx_agent._errors import ToolError

COMPLETED = {"command_id": "c1", "status": "EXECUTE_COMMAND_STATUS_COMPLETED",
             "exit_code": 0, "stdout": "4\n", "stderr": "", "truncated": False}


def _ws(*, states: list[str], exec_response: Any = COMPLETED, get_error: Exception | None = None,
        create_error: Exception | None = None) -> MagicMock:
    """A client whose Sandbox API walks ``states`` on GET and answers exec-sync with ``exec_response``."""
    ws = MagicMock()
    ws.config.workspace_id = None
    pending = list(states)
    get_errors = [get_error] if get_error is not None else []

    def do(method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST" and path == sb.SANDBOXES_PATH:
            if create_error is not None:
                raise create_error
            return {"name": f"sandboxes/{kwargs['query']['sandbox_id']}"}
        if method == "GET":
            if get_errors:
                raise get_errors.pop()
            return {"status": {"state": pending.pop(0) if len(pending) > 1 else pending[0]}}
        if "exec-sync" in path:
            if isinstance(exec_response, Exception):
                raise exec_response
            return exec_response
        return {}

    ws.api_client.do.side_effect = do
    return ws


def _calls(ws: MagicMock) -> list[tuple[str, str]]:
    return [c.args for c in ws.api_client.do.call_args_list]


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(sb, "_POLL_S", 0.001)


@pytest.fixture
def progress(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(sb, "emit_progress", lambda message, **a: events.append(message))
    return events


# ---------------------------------------------------------------------------
# Factory construction and binding
# ---------------------------------------------------------------------------


def test_factory_stamps_the_tool_and_binds_system_ai_sandbox_as_the_user():
    tool = sandbox_tool()

    assert tool.__name__ == "run_in_sandbox"
    assert "Databricks Sandbox" in tool.__doc__
    assert tool._apx_service_bindings == [ServiceBinding("system.ai.sandbox", "user")]
    assert infer_operation_authorization(tool).execution_identity == "user"


def test_app_auth_runs_as_the_app_service_principal():
    tool = sandbox_tool(auth="app", name="run_code")

    assert tool.__name__ == "run_code"
    assert tool._apx_service_bindings == [ServiceBinding("system.ai.sandbox", "app")]
    assert infer_operation_authorization(tool).execution_identity == "service"


@pytest.mark.parametrize("auth", ["user", "app"])
def test_llm_sees_code_and_a_language_enum_not_the_client(auth):
    from apx_agent._inspection import _inspect_tool_fn, _make_input_model, _schema_for_model

    tool = sandbox_tool(auth=auth)
    signature = _inspect_tool_fn(tool)
    schema = _schema_for_model(_make_input_model(tool, signature.plain_params))

    assert signature.dep_param_names == ["ws"]

    assert set(schema["properties"]) == {"code", "language"}
    assert schema["required"] == ["code"]
    assert schema["properties"]["language"]["enum"] == ["python", "shell"]


def test_named_sandbox_says_files_persist():
    assert "persist between calls" in sandbox_tool("scratch").__doc__
    assert "last one call" in sandbox_tool().__doc__


@pytest.mark.parametrize("kwargs", [{"auth": "sp"}, {"wait_s": 0}])
def test_factory_rejects_bad_arguments(kwargs):
    with pytest.raises(ValueError):
        sandbox_tool(**kwargs)


@pytest.mark.parametrize("args", [("main.ai.sandbox", "user"), ("system.ai.sandbox", "obo")])
def test_service_binding_rejects_non_system_services_and_unknown_auth(args):
    with pytest.raises(ValueError):
        ServiceBinding(*args)


def test_bindings_are_collected_across_the_agent_tree_and_deduplicated():
    agent = SequentialAgent([Agent(tools=[sandbox_tool()]), Agent(tools=[sandbox_tool(name="again")]),
                             Agent(tools=[sandbox_tool(auth="app", name="as_app")])])

    assert collect_service_bindings(agent) == [
        ServiceBinding("system.ai.sandbox", "user"), ServiceBinding("system.ai.sandbox", "app")]


def test_binding_adds_no_apps_resource():
    plan = compile_authorization_plan(Agent(tools=[sandbox_tool()]), model="")

    assert plan.user_resources == () and plan.service_resources == ()


# ---------------------------------------------------------------------------
# Execute path (mocked Sandbox API)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runs_python_in_a_throwaway_sandbox_and_deletes_it(progress, monkeypatch):
    def no_local_exec(*a: Any, **k: Any) -> None:
        raise AssertionError("generated code must not run in the App process")

    monkeypatch.setattr(subprocess, "Popen", no_local_exec)
    ws = _ws(states=["SANDBOX_STATE_PENDING", "SANDBOX_STATE_RUNNING"])

    result = await sandbox_tool()(code="print(2 + 2)", ws=ws)

    assert result == {"exit_code": 0, "stdout": "4\n", "stderr": "", "truncated": False}
    calls = _calls(ws)
    sandbox_id = ws.api_client.do.call_args_list[0].kwargs["query"]["sandbox_id"]
    assert sandbox_id.startswith("apx-")
    assert calls == [
        ("POST", "/api/2.0/sandboxes"),
        ("GET", f"/api/2.0/sandboxes/{sandbox_id}"),
        ("GET", f"/api/2.0/sandboxes/{sandbox_id}"),
        ("POST", f"/api/2.0/sandbox-exec/sandboxes/{sandbox_id}/exec-sync"),
        ("DELETE", f"/api/2.0/sandboxes/{sandbox_id}"),
    ]
    body = ws.api_client.do.call_args_list[3].kwargs["body"]
    assert body["cmd"] == "python3" and body["args"] == ["-c", "print(2 + 2)"]
    assert body["execution_timeout"].endswith("s") and int(body["execution_timeout"][:-1]) <= 300
    assert progress == ["Creating a Databricks Sandbox", "Running python in the sandbox"]


@pytest.mark.asyncio
async def test_shell_runs_through_bash():
    ws = _ws(states=["SANDBOX_STATE_RUNNING"])

    await sandbox_tool()(code="ls /home/sandbox-agent", language="shell", ws=ws)

    body = ws.api_client.do.call_args_list[2].kwargs["body"]
    assert body["cmd"] == "/bin/bash" and body["args"] == ["-c", "ls /home/sandbox-agent"]


@pytest.mark.asyncio
async def test_named_sandbox_is_created_on_first_use_and_kept():
    ws = _ws(states=["SANDBOX_STATE_RUNNING"], get_error=NotFound("sandbox not found"))

    await sandbox_tool("scratch")(code="print(1)", ws=ws)

    assert _calls(ws) == [
        ("GET", "/api/2.0/sandboxes/scratch"),
        ("POST", "/api/2.0/sandboxes"),
        ("GET", "/api/2.0/sandboxes/scratch"),
        ("POST", "/api/2.0/sandbox-exec/sandboxes/scratch/exec-sync"),
    ]
    assert ws.api_client.do.call_args_list[1].kwargs["query"] == {"sandbox_id": "scratch"}


@pytest.mark.asyncio
async def test_stopped_named_sandbox_is_started():
    ws = _ws(states=["SANDBOX_STATE_STOPPED", "SANDBOX_STATE_RUNNING"])

    await sandbox_tool("scratch")(code="print(1)", ws=ws)

    assert ("POST", "/api/2.0/sandboxes/scratch/start") in _calls(ws)
    assert not any(method == "DELETE" for method, _ in _calls(ws))


@pytest.mark.asyncio
async def test_non_zero_exit_is_a_result_the_model_can_read():
    failed = {**COMPLETED, "exit_code": 1, "stdout": "", "stderr": "NameError: x"}
    result = await sandbox_tool()(code="x", ws=_ws(states=["SANDBOX_STATE_RUNNING"], exec_response=failed))

    assert result["exit_code"] == 1 and result["stderr"] == "NameError: x"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "message"), [
    ("EXECUTE_COMMAND_STATUS_TIMED_OUT", "terminated"),
    ("EXECUTE_COMMAND_STATUS_FAILED", "did not run"),
])
async def test_timed_out_or_failed_command_is_a_tool_error(status, message):
    ws = _ws(states=["SANDBOX_STATE_RUNNING"], exec_response={"status": status})

    with pytest.raises(ToolError, match=message):
        await sandbox_tool()(code="while True: pass", ws=ws)
    assert _calls(ws)[-1][0] == "DELETE"


@pytest.mark.asyncio
async def test_sandbox_that_never_starts_is_bounded_by_wait_s_and_still_deleted():
    ws = _ws(states=["SANDBOX_STATE_PENDING"])

    with pytest.raises(ToolError, match="within 0s"):
        await sandbox_tool(wait_s=0.05)(code="print(1)", ws=ws)
    assert _calls(ws)[-1][0] == "DELETE"


@pytest.mark.asyncio
async def test_stale_obo_scope_is_a_reauthorize_tool_error():
    ws = _ws(states=["SANDBOX_STATE_RUNNING"],
             create_error=PermissionDenied("Invalid scope, required scopes: sandbox"))

    with pytest.raises(ToolError, match="Re-authorize the app"):
        await sandbox_tool()(code="print(1)", ws=ws)


@pytest.mark.asyncio
async def test_disabled_preview_is_a_tool_error_with_the_enable_step():
    ws = _ws(states=["SANDBOX_STATE_RUNNING"],
             create_error=DatabricksError("Sandbox is not enabled", error_code="FEATURE_DISABLED"))

    with pytest.raises(ToolError, match="Previews page"):
        await sandbox_tool()(code="print(1)", ws=ws)


@pytest.mark.asyncio
async def test_other_api_errors_are_tool_errors_not_500s():
    ws = _ws(states=["SANDBOX_STATE_RUNNING"], exec_response=DatabricksError("boom"))

    with pytest.raises(ToolError, match="Sandbox request failed: boom"):
        await sandbox_tool()(code="print(1)", ws=ws)


@pytest.mark.asyncio
async def test_unknown_language_is_a_tool_error():
    with pytest.raises(ToolError, match="language"):
        await sandbox_tool()(code="1", language="ruby", ws=_ws(states=["SANDBOX_STATE_RUNNING"]))


# ---------------------------------------------------------------------------
# apx-agent doctor
# ---------------------------------------------------------------------------


def test_doctor_ok_when_the_sandbox_api_answers():
    ws = MagicMock()
    ws.api_client.do.return_value = {"sandboxes": []}

    check = check_service_binding(ServiceBinding("system.ai.sandbox"), ws)

    assert check.status is Status.OK
    ws.api_client.do.assert_called_once_with("GET", "/api/2.0/sandboxes", query={"page_size": 1})


def test_doctor_fails_with_enable_steps_when_the_preview_is_off():
    ws = MagicMock()
    ws.api_client.do.side_effect = DatabricksError("Sandbox is not enabled", error_code="FEATURE_DISABLED")

    check = check_service_binding(ServiceBinding("system.ai.sandbox", "app"), ws)

    assert check.status is Status.FAIL
    assert check.name == "Service (system.ai.sandbox, auth=app)"
    assert check.fix is not None
    assert "Previews page" in check.fix and "Enhanced Unity Gateway" in check.fix


def test_doctor_warns_on_other_errors():
    ws = MagicMock()
    ws.api_client.do.side_effect = PermissionDenied("no access")

    assert check_service_binding(ServiceBinding("system.ai.sandbox"), ws).status is Status.WARN


def test_doctor_looks_up_other_services_as_unity_gateway_mcp_services():
    """Non-sandbox system.ai services are #1268's Unity Gateway MCP lookup."""
    ws = MagicMock()
    ws.api_client.do.return_value = {"name": "system.ai.web_search"}

    check = check_service_binding(ServiceBinding("system.ai.web_search"), ws)

    assert check.status is Status.OK
    ws.api_client.do.assert_called_once_with("GET", "/api/2.1/unity-catalog/mcp-services/system.ai.web_search")
