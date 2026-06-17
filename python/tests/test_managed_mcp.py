"""Tests for _managed_mcp.py — URLs and client config from declared resources.

Covers:
  1. URL templates per kind (uc_function / genie_space / vector_search_index).
  2. Host normalization (scheme injection, trailing slash trim).
  3. Unsupported kinds (serving_endpoint, sql_warehouse, uc_table) reported
     with url=None, unsupported=True.
  4. OAuth scope per kind matches the Databricks docs.
  5. managed_mcp_client_config produces the standard mcpServers shape;
     entries are keyed by f"{name}.{kind}.{identifier}".
  6. include_unsupported flag toggles inclusion of unsupported endpoints.
"""

from __future__ import annotations

import pytest

from apx_agent import (
    Agent,
    ManagedMCPEndpoint,
    ResourceSpec,
    genie_tool,
    managed_mcp_client_config,
    managed_mcp_urls,
    tool,
    uc_function_tool,
    vector_search_tool,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _multi_resource_agent() -> Agent:
    @tool(uc="main.tools.classify_intent")
    def classify_intent(query: str) -> str:
        """Classify."""
        return query

    return Agent(
        instructions="...",
        tools=[
            classify_intent,
            uc_function_tool("main.tools.score_customer"),
            genie_tool("space-abc-123"),
            vector_search_tool("main.search.docs_index"),
        ],
    )


# ---------------------------------------------------------------------------
# URL generation per kind
# ---------------------------------------------------------------------------


def test_uc_function_url_pattern() -> None:
    @tool(uc="main.tools.classify_intent")
    def classify(query: str) -> str:
        """."""
        return query

    agent = Agent(tools=[classify])
    endpoints = managed_mcp_urls(agent, workspace_host="https://workspace.cloud.databricks.com")
    uc_ep = next(e for e in endpoints if e.kind == "uc_function")
    assert uc_ep.url == (
        "https://workspace.cloud.databricks.com/api/2.0/mcp/functions/main/tools/classify_intent"
    )
    assert uc_ep.oauth_scope == "unity-catalog"
    assert uc_ep.unsupported is False


def test_genie_url_pattern() -> None:
    agent = Agent(tools=[genie_tool("space-abc-123")])
    endpoints = managed_mcp_urls(agent, workspace_host="https://workspace.cloud.databricks.com")
    genie_ep = next(e for e in endpoints if e.kind == "genie_space")
    assert genie_ep.url == (
        "https://workspace.cloud.databricks.com/api/2.0/mcp/genie/space-abc-123"
    )
    assert genie_ep.oauth_scope == "genie"


def test_vector_search_url_pattern() -> None:
    agent = Agent(tools=[vector_search_tool("main.search.docs_index")])
    endpoints = managed_mcp_urls(agent, workspace_host="https://workspace.cloud.databricks.com")
    vs_ep = next(e for e in endpoints if e.kind == "vector_search_index")
    assert vs_ep.url == (
        "https://workspace.cloud.databricks.com/api/2.0/mcp/vector-search/main/search/docs_index"
    )
    assert vs_ep.oauth_scope == "vector-search"


# ---------------------------------------------------------------------------
# Host normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "https://workspace.cloud.databricks.com",
        "https://workspace.cloud.databricks.com/",
        "workspace.cloud.databricks.com",
        "workspace.cloud.databricks.com/",
    ],
)
def test_host_normalised_consistently(host: str) -> None:
    @tool(uc="main.tools.f")
    def f(x: str) -> str:
        """."""
        return x

    agent = Agent(tools=[f])
    endpoints = managed_mcp_urls(agent, workspace_host=host)
    uc_ep = next(e for e in endpoints if e.kind == "uc_function")
    assert uc_ep.url == (
        "https://workspace.cloud.databricks.com/api/2.0/mcp/functions/main/tools/f"
    )


# ---------------------------------------------------------------------------
# Unsupported kinds
# ---------------------------------------------------------------------------


def test_serving_endpoint_marked_unsupported() -> None:
    # The model serving endpoint is auto-declared by collect_resource_specs
    # when model=... is passed — it's "unsupported" by Managed MCP today.
    @tool(uc="main.tools.f")
    def f(x: str) -> str:
        """."""
        return x

    agent = Agent(tools=[f])
    endpoints = managed_mcp_urls(
        agent,
        workspace_host="https://workspace.cloud.databricks.com",
        model="databricks-claude-sonnet-4-6",
    )
    serving = [e for e in endpoints if e.kind == "serving_endpoint"]
    assert len(serving) == 1
    assert serving[0].unsupported is True
    assert serving[0].url is None
    assert serving[0].oauth_scope is None


def test_uc_function_with_bad_identifier_marked_unsupported() -> None:
    """A ResourceSpec with a malformed UC name shouldn't yield a URL."""
    # Force a bad spec into an agent by attaching one to a plain function.
    from apx_agent import attach_resources

    def plain(x: str) -> str:
        """."""
        return x

    attach_resources(plain, [ResourceSpec("uc_function", "not_three_parts")])
    agent = Agent(tools=[plain])
    endpoints = managed_mcp_urls(agent, workspace_host="https://x.cloud.databricks.com")
    uc_eps = [e for e in endpoints if e.kind == "uc_function"]
    assert len(uc_eps) == 1
    assert uc_eps[0].url is None
    assert uc_eps[0].unsupported is True


# ---------------------------------------------------------------------------
# Composite walk
# ---------------------------------------------------------------------------


def test_walks_full_agent_resource_tree() -> None:
    agent = _multi_resource_agent()
    endpoints = managed_mcp_urls(agent, workspace_host="https://workspace.cloud.databricks.com")
    kinds = {e.kind for e in endpoints}
    assert "uc_function" in kinds
    assert "genie_space" in kinds
    assert "vector_search_index" in kinds


# ---------------------------------------------------------------------------
# Client config
# ---------------------------------------------------------------------------


def test_client_config_shape() -> None:
    eps = [
        ManagedMCPEndpoint(
            kind="uc_function",
            identifier="main.tools.classify",
            url="https://x.cloud.databricks.com/api/2.0/mcp/functions/main/tools/classify",
            oauth_scope="unity-catalog",
        ),
        ManagedMCPEndpoint(
            kind="genie_space",
            identifier="space-abc",
            url="https://x.cloud.databricks.com/api/2.0/mcp/genie/space-abc",
            oauth_scope="genie",
        ),
    ]
    config = managed_mcp_client_config(eps)
    assert "mcpServers" in config
    servers = config["mcpServers"]
    assert "databricks.uc_function.main.tools.classify" in servers
    assert servers["databricks.uc_function.main.tools.classify"]["type"] == "http"
    assert servers["databricks.uc_function.main.tools.classify"]["url"].endswith(
        "/api/2.0/mcp/functions/main/tools/classify"
    )
    assert servers["databricks.uc_function.main.tools.classify"]["oauth_scope"] == "unity-catalog"


def test_client_config_skips_unsupported_by_default() -> None:
    eps = [
        ManagedMCPEndpoint(
            kind="serving_endpoint",
            identifier="model-foo",
            url=None,
            oauth_scope=None,
            unsupported=True,
        ),
    ]
    config = managed_mcp_client_config(eps)
    assert config["mcpServers"] == {}


def test_client_config_includes_unsupported_when_requested() -> None:
    eps = [
        ManagedMCPEndpoint(
            kind="serving_endpoint",
            identifier="model-foo",
            url=None,
            oauth_scope=None,
            unsupported=True,
        ),
    ]
    config = managed_mcp_client_config(eps, include_unsupported=True)
    assert "databricks.serving_endpoint.model-foo" in config["mcpServers"]
    assert config["mcpServers"]["databricks.serving_endpoint.model-foo"]["unsupported"] is True


def test_client_config_custom_name_prefix() -> None:
    eps = [
        ManagedMCPEndpoint(
            kind="genie_space",
            identifier="space-abc",
            url="https://x/api/2.0/mcp/genie/space-abc",
            oauth_scope="genie",
        ),
    ]
    config = managed_mcp_client_config(eps, name="my-agent")
    assert "my-agent.genie_space.space-abc" in config["mcpServers"]


def test_managed_mcp_url_for_resource_single() -> None:
    """Per-resource wrapper builds the same URL managed_mcp_urls does."""
    from apx_agent._managed_mcp import managed_mcp_url_for_resource

    assert managed_mcp_url_for_resource(
        "uc_function", "main.tools.classify_intent",
        workspace_host="https://workspace.cloud.databricks.com",
    ) == "https://workspace.cloud.databricks.com/api/2.0/mcp/functions/main/tools/classify_intent"
    # scheme/trailing-slash are normalised
    assert managed_mcp_url_for_resource(
        "genie_space", "space-abc", workspace_host="workspace.cloud.databricks.com/",
    ) == "https://workspace.cloud.databricks.com/api/2.0/mcp/genie/space-abc"
    # kinds without a Managed MCP equivalent -> None
    assert managed_mcp_url_for_resource(
        "unknown_kind", "x", workspace_host="https://w.com",
    ) is None
