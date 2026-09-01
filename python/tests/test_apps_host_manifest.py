from __future__ import annotations

import json

import pytest

from apx_agent import (
    AgentConfig,
    Dependencies,
    LlmAgent,
    ResourceSpec,
    attach_resources,
    require_user_api_scopes,
    tool,
)
from apx_agent._apps_authorization import compile_authorization_plan
from apx_agent._apps_host_manifest import compile_apps_host_manifest


def test_manifest_projects_agent_tools_resources_and_scopes() -> None:
    def search_orders(
        ws: Dependencies.UserClient,
        query: str,
        limit: int = 10,
    ) -> str:
        """Search governed orders."""
        return query[:limit]

    def list_catalogs() -> str:
        """List visible catalogs."""
        return "main"

    @tool(execution="service")
    def refresh_telemetry(request: Dependencies.Request) -> str:
        """Refresh telemetry using the app identity."""
        return "ok"

    attach_resources(search_orders, [ResourceSpec("uc_table", "main.sales.orders")])
    require_user_api_scopes(list_catalogs, ["catalog.catalogs:read"])
    attach_resources(refresh_telemetry, [ResourceSpec("job", "telemetry-job")])
    agent = LlmAgent(
        name="orders",
        description="Order helper",
        instructions="Use governed data.",
        tools=[search_orders, list_catalogs, refresh_telemetry],
        temperature=0.2,
        max_tokens=512,
        max_iterations=4,
        sub_agents=["https://peer.cloud.databricksapps.com"],
    )
    plan = compile_authorization_plan(agent, model="model-a")

    manifest = compile_apps_host_manifest(
        agent,
        AgentConfig(
            name="orders",
            description="Order helper",
            model="model-a",
            instructions="Use governed data.",
            temperature=0.2,
            max_tokens=512,
            max_iterations=4,
        ),
    )

    assert manifest.kind == "apx.apps_host_manifest"
    assert manifest.version == 1
    assert manifest.agent.name == "orders"
    assert manifest.agent.description == "Order helper"
    assert manifest.agent.model == "model-a"
    assert manifest.agent.instructions == "Use governed data."
    tools = {item.name: item for item in manifest.tools}
    operations = {item.name: item for item in plan.operations}

    assert tools["search_orders"].runtime == "python"
    assert manifest.appkit.default is True
    assert manifest.appkit.tool_prefix == "apx."
    assert manifest.appkit.max_steps == 4
    assert manifest.appkit.limits.max_tool_calls == 4
    assert tools["search_orders"].parameters["properties"]["query"]["type"] == "string"
    assert tools["search_orders"].parameters["properties"]["limit"]["default"] == 10
    assert tools["search_orders"].annotations.effect == "update"
    assert tools["search_orders"].handler.kind == "python"
    assert tools["search_orders"].handler.ref.endswith(
        "test_apps_host_manifest:test_manifest_projects_agent_tools_resources_and_scopes.<locals>.search_orders"
    )
    assert [r.model_dump() for r in tools["search_orders"].resources] == [
        {"kind": "uc_table", "identifier": "main.sales.orders"}
    ]
    assert tools["search_orders"].user_api_scopes == ["sql"]
    assert tools["list_catalogs"].user_api_scopes == ["catalog.catalogs:read"]
    assert tools["refresh_telemetry"].user_api_scopes == []
    for name, operation in operations.items():
        annotation = tools[name].annotations
        assert annotation.execution_identity == operation.execution_identity
        assert annotation.requires_request_context == operation.requires_request_context
        assert annotation.requires_user_context == (
            operation.execution_identity == "user"
        )
    assert [r.model_dump() for r in manifest.resources] == [
        {"kind": "job", "identifier": "telemetry-job"},
        {"kind": "serving_endpoint", "identifier": "model-a"},
        {"kind": "uc_table", "identifier": "main.sales.orders"},
    ]
    assert [r.model_dump() for r in manifest.user_resources] == [
        {"kind": resource.kind, "identifier": resource.identifier}
        for resource in plan.user_resources
    ]
    assert [r.model_dump() for r in manifest.service_resources] == [
        {"kind": resource.kind, "identifier": resource.identifier}
        for resource in plan.service_resources
    ]
    assert manifest.user_api_scopes == list(plan.user_api_scopes)
    assert [permission.url for permission in manifest.app_to_app_permissions] == [
        dependency.url for dependency in plan.app_dependencies
    ]
    serialized = json.dumps(manifest.model_dump(mode="json"), sort_keys=True).lower()
    for forbidden in (
        "\"access_token\":",
        "\"token\":",
        "\"client_secret\":",
        "\"secret\":",
        "\"authorization\":",
        "\"headers\":",
    ):
        assert forbidden not in serialized


def test_manifest_compiles_one_authorization_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apx_agent._apps_host_manifest as host_manifest

    calls: list[str] = []
    original = host_manifest.compile_authorization_plan

    def compile_once(agent: LlmAgent, *, model: str):
        calls.append(model)
        return original(agent, model=model)

    monkeypatch.setattr(host_manifest, "compile_authorization_plan", compile_once)

    compile_apps_host_manifest(LlmAgent(tools=[]))

    assert calls == ["databricks-meta-llama-3-3-70b-instruct"]


def test_manifest_projects_explicit_effect_and_defaults_plain_tools() -> None:
    @tool(effect="read")
    def lookup(value: str) -> str:
        return value

    def apply(value: str) -> str:
        return value

    manifest = compile_apps_host_manifest(LlmAgent(tools=[lookup, apply]))
    effects = {item.name: item.annotations.effect for item in manifest.tools}
    apply_annotations = next(
        item.annotations for item in manifest.tools if item.name == "apply"
    )

    assert effects == {"lookup": "read", "apply": "update"}
    assert apply_annotations.execution_identity == "service"
    assert apply_annotations.requires_request_context is False
    assert apply_annotations.requires_user_context is False


def test_manifest_uses_instance_defaults_without_config() -> None:
    agent = LlmAgent(name="plain", tools=[], instructions="Help.")

    manifest = compile_apps_host_manifest(agent)

    assert manifest.agent.name == "plain"
    assert manifest.agent.instructions == "Help."
    assert manifest.agent.max_iterations == 10
    assert manifest.tools == []


def test_manifest_does_not_fetch_remote_cards() -> None:
    agent = LlmAgent(
        name="peer", tools=[], sub_agents=["https://peer.cloud.databricksapps.com"]
    )

    async def _raise() -> None:
        raise AssertionError("manifest compilation must stay offline")

    agent.fetch_remote_tools = _raise  # type: ignore[method-assign]

    manifest = compile_apps_host_manifest(agent)

    assert manifest.tools == []
    assert manifest.resources


def test_manifest_separates_apps_peers_from_resource_specs() -> None:
    agent = LlmAgent(
        name="peer",
        tools=[],
        sub_agents=[
            "endpoints/model-peer",
            "https://peer.cloud.databricksapps.com",
            "https://peer.cloud.databricksapps.com",
            "$APX_PEER_DYNAMIC_URL",
        ],
    )

    manifest = compile_apps_host_manifest(agent)

    assert [r.model_dump() for r in manifest.resources] == [
        {
            "kind": "serving_endpoint",
            "identifier": "databricks-meta-llama-3-3-70b-instruct",
        },
        {"kind": "serving_endpoint", "identifier": "model-peer"},
    ]
    assert [p.model_dump() for p in manifest.app_to_app_permissions] == [
        {"url": "https://peer.cloud.databricksapps.com", "permission": "CAN_USE"}
    ]
