from __future__ import annotations

from apx_agent import (
    AgentConfig,
    LlmAgent,
    ResourceSpec,
    attach_resources,
    require_user_api_scopes,
)
from apx_agent._apps_host_manifest import compile_apps_host_manifest


def test_manifest_projects_agent_tools_resources_and_scopes() -> None:
    def search_orders(query: str, limit: int = 10) -> str:
        """Search governed orders."""
        return query[:limit]

    def list_catalogs() -> str:
        """List visible catalogs."""
        return "main"

    attach_resources(search_orders, [ResourceSpec("uc_table", "main.sales.orders")])
    require_user_api_scopes(list_catalogs, ["catalog.catalogs:read"])
    agent = LlmAgent(
        name="orders",
        description="Order helper",
        instructions="Use governed data.",
        tools=[search_orders, list_catalogs],
        temperature=0.2,
        max_tokens=512,
        max_iterations=4,
    )

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
    assert manifest.tools[0].name == "search_orders"
    assert manifest.tools[0].runtime == "python"
    assert manifest.appkit.default is True
    assert manifest.appkit.tool_prefix == "apx."
    assert manifest.appkit.max_steps == 4
    assert manifest.appkit.limits.max_tool_calls == 4
    assert manifest.tools[0].parameters["properties"]["query"]["type"] == "string"
    assert manifest.tools[0].parameters["properties"]["limit"]["default"] == 10
    assert manifest.tools[0].annotations.effect == "read"
    assert manifest.tools[0].annotations.requires_user_context is True
    assert manifest.tools[0].handler.kind == "python"
    assert manifest.tools[0].handler.ref.endswith(
        "test_apps_host_manifest:test_manifest_projects_agent_tools_resources_and_scopes.<locals>.search_orders"
    )
    assert [r.model_dump() for r in manifest.tools[0].resources] == [
        {"kind": "uc_table", "identifier": "main.sales.orders"}
    ]
    assert manifest.tools[0].user_api_scopes == ["sql"]
    assert manifest.tools[1].user_api_scopes == ["catalog.catalogs:read"]
    assert [r.model_dump() for r in manifest.resources] == [
        {"kind": "serving_endpoint", "identifier": "model-a"},
        {"kind": "uc_table", "identifier": "main.sales.orders"},
    ]
    assert manifest.user_api_scopes == ["catalog.catalogs:read", "model-serving", "sql"]


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
