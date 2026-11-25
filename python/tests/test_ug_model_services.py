"""Unity Gateway model services as Apps resources and query targets (#1267).

Live inference, the Apps MODEL_SERVICE preview and privilege auto-grants need a
workspace; these tests cover resource emission and client wiring with a mocked
gateway.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from databricks.sdk.errors.base import DatabricksError

from apx_agent import Agent, ResourceSpec, foundation_model_tool
from apx_agent._agentbricks_deploy import native_model_services
from apx_agent._apps_authorization import (
    AppFamilyPermissions,
    compile_authorization_plan,
    infer_operation_authorization,
)
from apx_agent._doctor import Status, check_model_service
from apx_agent._llm import AI_GATEWAY_CHAT_PATH, model_service_name
from apx_agent._resources import get_resources, resources_to_databricks_yml

SERVICE = "system.ai.claude-sonnet-4-5"


# --- naming ------------------------------------------------------------------


@pytest.mark.parametrize(("model", "service"), [
    ("system.ai.claude-sonnet-4-5", "system.ai.claude-sonnet-4-5"),
    ("main.agents.router", "main.agents.router"),
    ("databricks-claude-sonnet-4-6", "system.ai.claude-sonnet-4-6"),
    ("my-custom-endpoint", None),
    ("main.agents", None),
    ("", None),
])
def test_model_service_name(model: str, service: str | None) -> None:
    assert model_service_name(model) == service


# --- resource emission ---------------------------------------------------------


def test_model_service_projects_to_uc_securable_execute() -> None:
    [entry] = resources_to_databricks_yml([ResourceSpec("model_service", SERVICE)])
    assert entry["uc_securable"] == {
        "securable_full_name": SERVICE,
        "securable_type": "MODEL_SERVICE",
        "permission": "EXECUTE",
    }
    assert 2 <= len(entry["name"]) <= 30


@pytest.mark.parametrize("model", ["system.ai.claude-sonnet-4-5", "main.agents.router"])
def test_chat_model_service_is_an_app_sp_resource(model: str) -> None:
    plan = compile_authorization_plan(Agent(), model=model)
    assert ResourceSpec("model_service", model) in plan.service_resources
    assert "ai-gateway" in plan.user_api_scopes


def test_non_service_chat_model_emits_no_model_service() -> None:
    plan = compile_authorization_plan(Agent(), model="my-custom-endpoint")
    assert not any(r.kind == "model_service" for r in plan.service_resources)


def test_tool_model_service_emitted_alongside_chat_model() -> None:
    plan = compile_authorization_plan(
        Agent(tools=[foundation_model_tool("main.agents.router", name="ask_router")]),
        model=SERVICE,
    )
    services = [r.identifier for r in plan.service_resources if r.kind == "model_service"]
    assert sorted(services) == ["main.agents.router", SERVICE]
    entries = resources_to_databricks_yml(plan.service_resources)
    assert {e["uc_securable"]["securable_full_name"] for e in entries} == set(services)


def test_apps_api_grant_update_keeps_model_service_type() -> None:
    """The pinned SDK maps the unknown MODEL_SERVICE enum to None; the update must not."""
    from apx_agent._apps_api_deploy import _apply_grants

    plan = compile_authorization_plan(Agent(), model=SERVICE)
    ws = MagicMock()
    _apply_grants(ws, app_name="proof", space=None, profile=None, plan=plan,
                  family_permissions=AppFamilyPermissions(), log=lambda _m: None)
    app = ws.apps.create_update.call_args.kwargs["app"]
    [resource] = app.as_dict()["resources"]
    assert resource["uc_securable"]["securable_type"] == "MODEL_SERVICE"
    assert resource["uc_securable"]["securable_full_name"] == SERVICE


def test_native_model_services_roles() -> None:
    roles = native_model_services(["main.agents.router", SERVICE, "main.other.router"], SERVICE)
    assert roles == {
        "agent": SERVICE,
        "router": "main.agents.router",
        "router_2": "main.other.router",
    }
    assert native_model_services(["main.agents.9b"], None) == {"model_9b": "main.agents.9b"}


# --- foundation_model_tool over Unity Gateway ----------------------------------


def test_service_tool_declares_model_service_and_runs_as_app() -> None:
    fn = foundation_model_tool(SERVICE)
    assert get_resources(fn) == [ResourceSpec("model_service", SERVICE)]
    assert infer_operation_authorization(fn).execution_identity == "service"


def test_service_tool_rejects_malformed_name() -> None:
    with pytest.raises(ValueError, match="catalog.schema.service"):
        foundation_model_tool("main.router")


@pytest.mark.asyncio
async def test_service_tool_posts_three_part_name_to_gateway() -> None:
    fn = foundation_model_tool(SERVICE, system_prompt="Be terse.", temperature=0.1, max_tokens=64)
    ws = MagicMock()
    ws.api_client.do.return_value = {"choices": [{"message": {"content": "42"}}]}

    assert await fn(prompt="answer?", ws=ws) == "42"

    ws.api_client.do.assert_called_once_with("POST", AI_GATEWAY_CHAT_PATH, body={
        "model": SERVICE,
        "messages": [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "answer?"},
        ],
        "temperature": 0.1,
        "max_tokens": 64,
    })
    ws.serving_endpoints.query.assert_not_called()


@pytest.mark.asyncio
async def test_service_tool_empty_choices_returns_empty() -> None:
    ws = MagicMock()
    ws.api_client.do.return_value = {"choices": []}
    assert await foundation_model_tool(SERVICE)(prompt="hi", ws=ws) == ""


@pytest.mark.parametrize(("error_code", "message", "expect"), [
    ("PERMISSION_DENIED", "User does not have EXECUTE", "CAN_QUERY grant does not grant"),
    ("NOT_FOUND", "Model service does not exist", "was not found"),
    ("FEATURE_DISABLED", "Unity Gateway is not enabled", "Unity Gateway is unavailable"),
])
@pytest.mark.asyncio
async def test_service_tool_explains_gateway_errors(error_code: str, message: str, expect: str) -> None:
    ws = MagicMock()
    ws.api_client.do.side_effect = DatabricksError(message, error_code=error_code)
    result = await foundation_model_tool(SERVICE)(prompt="hi", ws=ws)
    assert expect in result
    assert SERVICE in result


def test_databricks_name_rewrites_to_system_ai_service() -> None:
    fn = foundation_model_tool("databricks-claude-opus-4-7")
    assert get_resources(fn) == [ResourceSpec("model_service", "system.ai.claude-opus-4-7")]
    assert infer_operation_authorization(fn).execution_identity == "service"
    assert not any(r.kind == "serving_endpoint" for r in get_resources(fn))


@pytest.mark.asyncio
async def test_databricks_name_posts_system_ai_to_gateway() -> None:
    ws = MagicMock()
    ws.api_client.do.return_value = {"choices": [{"message": {"content": "ok"}}]}
    assert await foundation_model_tool("databricks-claude-opus-4-7")(prompt="hi", ws=ws) == "ok"
    ws.api_client.do.assert_called_once_with("POST", AI_GATEWAY_CHAT_PATH, body={
        "model": "system.ai.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
    })
    ws.serving_endpoints.query.assert_not_called()


def test_databricks_tool_compiles_to_model_service_grant() -> None:
    plan = compile_authorization_plan(
        Agent(tools=[foundation_model_tool("databricks-claude-opus-4-7", name="ask_opus")]),
        model=SERVICE,
    )
    assert ResourceSpec("model_service", "system.ai.claude-opus-4-7") in plan.service_resources
    assert not any(r.kind == "serving_endpoint" for r in plan.service_resources)
    [entry] = [
        e for e in resources_to_databricks_yml(plan.service_resources)
        if e["uc_securable"]["securable_full_name"] == "system.ai.claude-opus-4-7"
    ]
    assert entry["uc_securable"]["securable_type"] == "MODEL_SERVICE"
    assert entry["uc_securable"]["permission"] == "EXECUTE"


def test_custom_endpoint_name_fails_at_factory_time() -> None:
    with pytest.raises(ValueError, match="declare a Unity Gateway model service"):
        foundation_model_tool("my-custom-endpoint")


# --- doctor --------------------------------------------------------------------


def test_doctor_model_service_ok() -> None:
    ws = MagicMock()
    ws.api_client.do.return_value = {"name": SERVICE}
    check = check_model_service(SERVICE, ws)
    assert check.status is Status.OK
    ws.api_client.do.assert_called_once_with("GET", f"/api/2.1/unity-catalog/model-services/{SERVICE}")


@pytest.mark.parametrize(("error", "expect"), [
    (DatabricksError("no such service", error_code="NOT_FOUND"), "was not found"),
    (DatabricksError("missing privilege", error_code="PERMISSION_DENIED"), "needs EXECUTE"),
    (DatabricksError("region", error_code="FEATURE_DISABLED"), "Unity Gateway is unavailable"),
])
def test_doctor_model_service_failures_are_specific(error: Exception, expect: str) -> None:
    ws = MagicMock()
    ws.api_client.do.side_effect = error
    check = check_model_service(SERVICE, ws)
    assert check.status is Status.FAIL
    assert expect in check.detail
    assert check.fix is not None and "MODEL_SERVICE" in check.fix


def test_doctor_model_service_unclassified_error_warns() -> None:
    ws = MagicMock()
    ws.api_client.do.side_effect = ConnectionError("network down")
    assert check_model_service(SERVICE, ws).status is Status.WARN
