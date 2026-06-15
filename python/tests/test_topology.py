"""Tests for _topology.py — discover + render the multi-agent graph.

Covers:
  - discover_topology pulls AgentNodes for apx-tagged models and edges
    from apx.agent.metadata.sub_agents
  - Apps URLs vs endpoint refs vs bare names get classified correctly
  - Models without apx.agent.name tags are skipped
  - render_topology mermaid and graphviz outputs include all nodes + edges
  - Sub-agent targets that don't have their own node show up as external
    targets in the rendered graph
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apx_agent import (
    AgentNode,
    Topology,
    TopologyEdge,
    discover_topology,
    render_topology,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _model(name: str, *, tags: dict[str, str], catalog: str = "main", schema: str = "agents") -> object:
    return SimpleNamespace(
        name=name,
        catalog_name=catalog,
        schema_name=schema,
        full_name=f"{catalog}.{schema}.{name}",
        tags=[SimpleNamespace(key=k, value=v) for k, v in tags.items()],
    )


def _make_ws(models: list[object]) -> MagicMock:
    ws = MagicMock()
    ws.registered_models.list.return_value = iter(models)
    return ws


# ---------------------------------------------------------------------------
# discover_topology
# ---------------------------------------------------------------------------


def test_discover_pulls_apx_tagged_models_as_nodes() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.model": "databricks-claude-sonnet-4-6",
            "apx.agent.tool_count": "3",
            "apx.agent.metadata": json.dumps({
                "sub_agents": [],
                "resources": [{"kind": "uc_function", "identifier": "main.tools.x"}],
            }),
        }),
        _model("untagged", tags={"other.tag": "x"}),  # should not appear
    ])

    topo = discover_topology(ws)

    assert len(topo.nodes) == 1
    node = topo.nodes[0]
    assert node.name == "triage"
    assert node.model_endpoint == "databricks-claude-sonnet-4-6"
    assert node.tool_count == 3
    assert "uc_function" in node.resource_kinds


def test_discover_extracts_edges_from_metadata_sub_agents() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.metadata": json.dumps({
                "sub_agents": [
                    "endpoints/billing",
                    "serving-endpoints/technical",
                    "data-triage",  # bare name
                ],
            }),
        }),
    ])

    topo = discover_topology(ws)

    assert len(topo.edges) == 3
    target_names = {e.target for e in topo.edges}
    assert target_names == {"billing", "technical", "data-triage"}
    for e in topo.edges:
        assert e.source == "triage"
        assert e.target_kind == "endpoint"


def test_discover_classifies_apps_urls_separately() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.metadata": json.dumps({
                "sub_agents": [
                    "https://billing.workspace.databricksapps.com",
                ],
            }),
        }),
    ])

    topo = discover_topology(ws)

    assert len(topo.edges) == 1
    e = topo.edges[0]
    assert e.target_kind == "app_url"
    assert "billing.workspace.databricksapps.com" in e.target


def test_discover_falls_back_to_csv_sub_agents_tag() -> None:
    """When metadata doesn't have sub_agents, the comma-separated tag is read."""
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.sub_agents": "endpoints/billing,endpoints/technical",
        }),
    ])

    topo = discover_topology(ws)

    target_names = {e.target for e in topo.edges}
    assert target_names == {"billing", "technical"}


def test_discover_handles_malformed_metadata_json() -> None:
    ws = _make_ws([
        _model("triage", tags={
            "apx.agent.name": "triage",
            "apx.agent.metadata": "not valid json[",
        }),
    ])

    topo = discover_topology(ws)
    assert len(topo.nodes) == 1
    assert topo.edges == ()


# ---------------------------------------------------------------------------
# render_topology
# ---------------------------------------------------------------------------


def _sample_topology() -> Topology:
    return Topology(
        nodes=(
            AgentNode(
                name="triage",
                uc_name="main.agents.triage",
                model_endpoint="databricks-claude-sonnet-4-6",
                tool_count=3,
                resource_kinds=("uc_function", "genie_space"),
            ),
            AgentNode(
                name="billing",
                uc_name="main.agents.billing",
                model_endpoint="databricks-claude-sonnet-4-6",
            ),
        ),
        edges=(
            TopologyEdge(source="triage", target="billing", target_kind="endpoint"),
            TopologyEdge(source="triage", target="external_lookup", target_kind="endpoint"),
        ),
    )


def test_render_mermaid_includes_nodes_and_edges() -> None:
    text = render_topology(_sample_topology(), format="mermaid")
    assert text.startswith("graph LR")
    assert "triage" in text
    assert "billing" in text
    assert "-->" in text
    # External (not in nodes) shows up too
    assert "external_lookup" in text


def test_render_mermaid_embeds_model_endpoint_as_subtext() -> None:
    text = render_topology(_sample_topology(), format="mermaid")
    assert "databricks-claude-sonnet-4-6" in text


def test_render_graphviz_emits_digraph() -> None:
    text = render_topology(_sample_topology(), format="graphviz")
    assert text.startswith("digraph apx_topology")
    assert '"triage"' in text
    assert '"billing"' in text
    assert '"triage" -> "billing"' in text


def test_render_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="Unknown format"):
        render_topology(_sample_topology(), format="svg")  # type: ignore[arg-type]


def test_render_handles_empty_topology() -> None:
    topo = Topology(nodes=(), edges=())
    mermaid = render_topology(topo, format="mermaid")
    assert mermaid.startswith("graph LR")
    graphviz = render_topology(topo, format="graphviz")
    assert graphviz.startswith("digraph apx_topology")


# ---------------------------------------------------------------------------
# build_topology / inspect_node — in-process agent-tree topology
#
# These exercise the dev-UI ``/_apx/topology`` data layer. ``build_topology``
# walks a live ``BaseAgent`` tree and ``inspect_node`` returns details for a
# given stable node ID.
# ---------------------------------------------------------------------------


from apx_agent import Agent, AgentConfig, AgentContext, HandoffAgent  # noqa: E402
from apx_agent._models import AgentCard  # noqa: E402
from apx_agent._resources import ResourceSpec, attach_resources  # noqa: E402
from apx_agent._topology import build_topology, inspect_node  # noqa: E402


def _make_ctx(agent, *, name: str = "test-agent", model: str = "databricks-claude-sonnet-4-6") -> AgentContext:
    cfg = AgentConfig(name=name, model=model, instructions="You are helpful.")
    tools = agent.collect_tools()
    card = AgentCard(name=name, description="")
    return AgentContext(config=cfg, tools=tools, card=card, agent=agent)


def _uc_tool(name: str, uc_full_name: str):
    """Mimic what ``uc_function_tool`` returns — a callable with _apx_resources."""
    def _fn(x: int) -> str:
        """Call the UC function."""
        return str(x)
    _fn.__name__ = name
    _fn.__qualname__ = name
    attach_resources(_fn, [ResourceSpec("uc_function", uc_full_name)])
    return _fn


def test_build_topology_llm_agent_with_uc_function_tools() -> None:
    classify = _uc_tool("classify_intent", "main.tools.classify_intent")
    lookup = _uc_tool("lookup_account", "main.tools.lookup_account")
    agent = Agent(tools=[classify, lookup], instructions="Route customer support questions.")
    ctx = _make_ctx(agent, name="support-agent")

    topo = build_topology(ctx)

    assert topo["rootId"] == "agent:root"
    assert topo["agentName"] == "support-agent"

    nodes_by_id = {n["id"]: n for n in topo["nodes"]}
    # Root agent + 2 tool nodes + 2 UC function resource nodes + 1 model endpoint
    assert "agent:root" in nodes_by_id
    assert nodes_by_id["agent:root"]["type"] == "LlmAgent"

    assert "tool:agent:root:classify_intent" in nodes_by_id
    assert "tool:agent:root:lookup_account" in nodes_by_id

    assert "uc:main.tools.classify_intent" in nodes_by_id
    assert "uc:main.tools.lookup_account" in nodes_by_id
    assert nodes_by_id["uc:main.tools.classify_intent"]["type"] == "UCFunction"

    # Edges: root --uses-tool--> each tool; each tool --delegates-to--> its UC fn;
    # root --calls-model--> endpoint
    edge_triples = {(e["source"], e["target"], e["kind"]) for e in topo["edges"]}
    assert ("agent:root", "tool:agent:root:classify_intent", "uses-tool") in edge_triples
    assert ("agent:root", "tool:agent:root:lookup_account", "uses-tool") in edge_triples
    assert (
        "tool:agent:root:classify_intent",
        "uc:main.tools.classify_intent",
        "delegates-to",
    ) in edge_triples
    assert (
        "tool:agent:root:lookup_account",
        "uc:main.tools.lookup_account",
        "delegates-to",
    ) in edge_triples
    assert (
        "agent:root",
        "endpoint:databricks-claude-sonnet-4-6",
        "calls-model",
    ) in edge_triples


def test_build_topology_handoff_agent_with_sub_agents() -> None:
    billing = Agent(tools=[], instructions="You handle billing.")
    technical = Agent(tools=[], instructions="You handle technical issues.")
    triage = HandoffAgent(agents={"billing": billing, "technical": technical}, start="billing")
    ctx = _make_ctx(triage, name="triage")

    topo = build_topology(ctx)

    nodes_by_id = {n["id"]: n for n in topo["nodes"]}
    assert "agent:root" in nodes_by_id
    assert nodes_by_id["agent:root"]["type"] == "HandoffAgent"
    # Both sub-agents present
    assert "agent:root.billing" in nodes_by_id
    assert "agent:root.technical" in nodes_by_id
    assert nodes_by_id["agent:root.billing"]["type"] == "LlmAgent"

    edge_triples = {(e["source"], e["target"], e["kind"]) for e in topo["edges"]}
    assert ("agent:root", "agent:root.billing", "branch") in edge_triples
    assert ("agent:root", "agent:root.technical", "branch") in edge_triples


def test_inspect_node_returns_agent_tool_and_uc_function_details() -> None:
    classify = _uc_tool("classify_intent", "main.tools.classify_intent")
    agent = Agent(
        tools=[classify],
        instructions="Route customer questions.",
        max_iterations=7,
    )
    ctx = _make_ctx(agent, name="support-agent")

    # Agent detail
    agent_detail = inspect_node(ctx, "agent:root")
    assert agent_detail is not None
    assert agent_detail["id"] == "agent:root"
    assert agent_detail["type"] == "LlmAgent"
    assert agent_detail["label"] == "support-agent"
    assert agent_detail["agent"]["className"] == "LlmAgent"
    assert agent_detail["agent"]["toolCount"] == 1
    assert agent_detail["agent"]["subAgentCount"] == 0
    assert agent_detail["agent"]["model"] == "databricks-claude-sonnet-4-6"
    assert agent_detail["agent"]["maxIterations"] == 7
    assert "Route" in (agent_detail["agent"]["instructions"] or "")

    # Tool detail
    tool_detail = inspect_node(ctx, "tool:agent:root:classify_intent")
    assert tool_detail is not None
    assert tool_detail["id"] == "tool:agent:root:classify_intent"
    assert tool_detail["type"] == "UCFunction"  # single-resource tool inherits resource node-type
    assert tool_detail["tool"]["name"] == "classify_intent"
    assert tool_detail["tool"]["isSync"] is True
    assert "hasObOTokenDep" in tool_detail["tool"]

    # UC function detail
    uc_detail = inspect_node(ctx, "uc:main.tools.classify_intent")
    assert uc_detail is not None
    assert uc_detail["type"] == "UCFunction"
    assert uc_detail["label"] == "main.tools.classify_intent"
    assert uc_detail["resource"]["resourceKind"] == "uc_function"
    assert uc_detail["resource"]["identifier"] == "main.tools.classify_intent"


def test_inspect_node_returns_none_for_unknown_id() -> None:
    agent = Agent(tools=[], instructions="hi")
    ctx = _make_ctx(agent)
    assert inspect_node(ctx, "agent:root.does_not_exist") is None
    assert inspect_node(ctx, "tool:agent:root:missing") is None
    assert inspect_node(ctx, "") is None
    assert inspect_node(ctx, "unknownprefix:foo") is None


def test_inspect_node_adds_resource_url_when_host_set(monkeypatch) -> None:
    """Regression: the Managed MCP resource URL must actually populate.

    _resource_url_for previously imported a never-written function and always
    returned None, so the topology 'url' field was silently dead. With
    DATABRICKS_HOST set it must now resolve.
    """
    monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.cloud.databricks.com")
    classify = _uc_tool("classify_intent", "main.tools.classify_intent")
    ctx = _make_ctx(Agent(tools=[classify], instructions="x"))

    node = inspect_node(ctx, "uc:main.tools.classify_intent")
    assert node is not None
    assert node["resource"]["url"] == (
        "https://workspace.cloud.databricks.com/api/2.0/mcp/functions/main/tools/classify_intent"
    )


def test_inspect_node_omits_resource_url_without_host(monkeypatch) -> None:
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    classify = _uc_tool("classify_intent", "main.tools.classify_intent")
    ctx = _make_ctx(Agent(tools=[classify], instructions="x"))

    node = inspect_node(ctx, "uc:main.tools.classify_intent")
    assert node is not None
    assert "url" not in node["resource"]
